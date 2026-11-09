"""Bounded acquisition from explicit inventories; no cloned code is executed.

Git reads an operator-supplied local checkout by full commit. Remote code/docs
use approved HTTPS prefixes and content hashes. This is not a whole-web crawler.
"""
from __future__ import annotations

import http.client
import ipaddress
import os
import selectors
import socket
import ssl
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

from kernelcat_ingestion.plans import Asset, SourceSpec, public_url, relative_path
from kernelcat_ingestion.snapshots import DataError


@dataclass(frozen=True)
class Acquisition:
    content: bytes
    origin: str
    requests: int = 0


def local_bytes(root: Path, name: str, cap: int) -> bytes:
    parts = Path(relative_path(name)).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise DataError("source is not a regular file")
            content = stream.read(cap + 1)
        if len(content) > cap:
            raise DataError("source exceeds file byte limit")
        return content
    finally:
        os.close(fd)


def git_output(root: Path, args: list[str], cap: int, timeout: int = 20) -> bytes:
    command = ["git", "--no-pager", "--no-replace-objects", "--literal-pathspecs",
               "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C",
           # Read-only acquisition must not fetch missing promisor objects or
           # follow any transport, even if repository config enables it.
           "GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
    assert process.stdout is not None
    content = bytearray()
    end = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as poll:
            poll.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0 or not poll.select(remaining):
                    raise DataError("Git read timeout")
                part = os.read(process.stdout.fileno(), min(65536, cap + 1 - len(content)))
                if not part:
                    break
                content.extend(part)
                if len(content) > cap:
                    raise DataError("Git response exceeds byte limit")
        if process.wait(timeout=max(.01, end - time.monotonic())):
            raise DataError("Git object unavailable")
        return bytes(content)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def git_bytes(spec: SourceSpec, asset: Asset, cap: int) -> Acquisition:
    root = Path(spec.location).resolve(strict=True)
    commit = git_output(root, ["rev-parse", "--verify", spec.revision + "^{commit}"], 128).decode().strip()
    if commit != spec.revision:
        raise DataError("Git commit pin mismatch")
    listing = git_output(root, ["ls-tree", "-z", spec.revision, "--", asset.path], 16384)
    rows = [r for r in listing.split(b"\0") if r]
    if len(rows) != 1:
        raise DataError("missing or ambiguous Git asset")
    info, name = rows[0].split(b"\t", 1)
    mode, kind, oid = info.decode().split()
    if name.decode() != asset.path or mode not in {"100644", "100755"} or kind != "blob":
        raise DataError("Git asset is a directory, symlink or submodule, not collected bytes")
    size = int(git_output(root, ["cat-file", "-s", oid], 128))
    if size > cap:
        raise DataError("Git blob exceeds file byte limit")
    content = git_output(root, ["cat-file", "blob", oid], cap)
    if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        raise DataError("Git LFS pointer is not materialized content")
    return Acquisition(content, "git+" + root.as_uri() + "@" + spec.revision + "/" + asset.path)


def _public_address(host: str) -> str:
    addresses = [str(row[4][0]) for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise DataError("HTTPS target resolved outside the public network")
    return addresses[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, *, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(host, timeout=timeout, context=context)
        self.tls_context = context

    def connect(self) -> None:
        # Resolve once and connect to that checked address; TLS still verifies the
        # original host. No proxy, ambient credentials, or second DNS lookup.
        address = _public_address(self.host)
        raw = socket.create_connection((address, self.port), self.timeout)
        try:
            self.sock = self.tls_context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def https_bytes(spec: SourceSpec, asset: Asset, cap: int) -> Acquisition:
    url = spec.location + quote(asset.path, safe="/")
    original = url
    deadline = time.monotonic() + 30
    for attempt in range(4):
        public_url(url)
        if not url.startswith(spec.location):
            raise DataError("redirect escaped approved source prefix")
        p = urlsplit(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DataError("HTTPS acquisition deadline")
        connection = _PinnedHTTPS(p.hostname or "", timeout=min(10, remaining),
                                  context=ssl.create_default_context())
        try:
            connection.request("GET", p.path or "/", headers={"Accept-Encoding": "identity",
                               "User-Agent": "KernelCat-Data/0.1"})
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise DataError("redirect without target")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                # No implicit retries. A 429/5xx is a recorded failed attempt.
                raise DataError(f"HTTP status {response.status}")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise DataError("compressed HTTP response not accepted by bounded reader")
            length = response.getheader("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > cap):
                raise DataError("invalid or excessive HTTP content length")
            content = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DataError("HTTPS acquisition deadline")
                if connection.sock is not None:
                    connection.sock.settimeout(min(10, remaining))
                part = response.read1(min(65536, cap + 1 - len(content)))
                if not part:
                    break
                content.extend(part)
                if len(content) > cap:
                    raise DataError("HTTP body exceeds byte limit")
            if length is not None and len(content) != int(length):
                raise DataError("truncated HTTP body")
            return Acquisition(bytes(content), original, attempt + 1)
        finally:
            connection.close()
    raise DataError("HTTPS redirect limit reached")


def acquire(spec: SourceSpec, asset: Asset, cap: int) -> Acquisition:
    if spec.kind == "git":
        return git_bytes(spec, asset, cap)
    if spec.kind == "https":
        return https_bytes(spec, asset, cap)
    root = Path(spec.location).resolve(strict=True)
    content = local_bytes(root, asset.path, cap)
    return Acquisition(content, spec.kind + "+" + root.as_uri() + "/" + asset.path)

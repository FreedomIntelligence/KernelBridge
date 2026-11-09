"""Shared, no-follow filesystem operations for immutable corpus catalogs."""
from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import local
from uuid import uuid4


class _HeldLocks(local):
    def __init__(self) -> None:
        self.keys: set[tuple[int, int, int]] = set()


_held = _HeldLocks()
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Walk each component using directory descriptors, never resolve symlinks."""
    absolute = Path(os.path.abspath(path))
    fd = os.open(absolute.anchor, _DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, _DIRECTORY, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
                child = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def check_directory(path: Path) -> None:
    """Validate existing components without creating a missing directory."""
    try:
        with directory(path):
            pass
    except FileNotFoundError:
        pass


def _read_at(parent: int, name: str, limit: int | None) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError(errno.EINVAL, "catalog entry must be a regular file", name)
        content = stream.read() if limit is None else stream.read(limit + 1)
    if limit is not None and len(content) > limit:
        raise OSError(errno.EFBIG, "catalog entry exceeds its byte limit", name)
    return content


def read_bytes(path: Path, limit: int | None = None) -> bytes:
    with directory(path.parent) as parent:
        return _read_at(parent, path.name, limit)


def record_names(path: Path) -> list[str]:
    try:
        with directory(path) as parent:
            return sorted(name for name in os.listdir(parent) if name.endswith(".json"))
    except FileNotFoundError:
        return []


def _matches(parent: int, name: str, content: bytes) -> bool:
    try:
        return _read_at(parent, name, len(content)) == content
    except OSError as exc:
        if exc.errno == errno.EFBIG:
            return False
        raise


def install(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Durably create a complete file, or verify equal bytes without replacing it."""
    with directory(path.parent, create=True) as parent:
        try:
            matches = _matches(parent, path.name, content)
        except FileNotFoundError:
            pass
        else:
            if not matches:
                raise FileExistsError("immutable object conflict")
            os.fsync(parent)
            return
        name = ".write-" + uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     mode, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(name, path.name, src_dir_fd=parent, dst_dir_fd=parent,
                        follow_symlinks=False)
            except FileExistsError:
                if not _matches(parent, path.name, content):
                    raise FileExistsError("immutable object conflict") from None
        finally:
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)


def _open_lock(parent: int) -> int:
    # A late non-exclusive O_CREAT can fail on Darwin even when the lock exists.
    # Never replace that inode: all writers must flock the same persistent file.
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        return os.open(".lock", flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
    except FileExistsError:
        return os.open(".lock", flags, dir_fd=parent)


@contextmanager
def catalog_lock(root: Path) -> Iterator[None]:
    """One lock shared by every writer, reentrant only in the owning process/thread."""
    with directory(root, create=True) as parent:
        identity = os.fstat(parent)
        key = (os.getpid(), identity.st_dev, identity.st_ino)
        if key in _held.keys:
            yield
            return
        fd = _open_lock(parent)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EINVAL, "catalog lock must be a regular file")
            fcntl.flock(fd, fcntl.LOCK_EX)
            _held.keys.add(key)
            try:
                yield
            finally:
                _held.keys.remove(key)
        finally:
            os.close(fd)


@contextmanager
def catalog_locks(roots: Iterable[Path]) -> Iterator[None]:
    """Acquire existing owners in physical directory order, independent of roles.

    Directory creation/inspection precedes locking and opens no metadata transaction.
    Duplicate physical owners share the original lock and its reentrant behavior.
    """
    owners: dict[tuple[int, int], Path] = {}
    for root in roots:
        with directory(root, create=True) as parent:
            identity = os.fstat(parent)
            owners.setdefault((identity.st_dev, identity.st_ino), root)
    with ExitStack() as stack:
        for identity in sorted(owners):
            stack.enter_context(catalog_lock(owners[identity]))
        yield

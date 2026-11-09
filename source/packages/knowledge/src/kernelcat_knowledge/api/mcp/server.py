"""Thin MCP compatibility adapter over the CLI-first Wiki application core."""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from kernelcat_knowledge.api.application import WikiApplication
from kernelcat_knowledge.api.bootstrap import (
    require_knowledge_admission,
    standalone_application,
)
from kernelcat_knowledge.api.mcp import config
from kernelcat_knowledge.api.mcp.help_doc import help_payload
from kernelcat_knowledge.query.filters import AliasResolver
from kernelcat_knowledge.query.legacy import search_enveloped
from kernelcat_knowledge.storage.legacy_trajectory import save_trajectory

_stats = {"total": 0, "ok": 0, "err": 0, "since_heartbeat": 0}
_stats_lock = threading.Lock()


def _log(message: str) -> None:
    print(f"[mcp {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def _bump(*keys: str) -> None:
    with _stats_lock:
        for key in keys:
            _stats[key] += 1


async def _track_async(name: str, summary: str, awaitable):
    _log(f"{name} start: {summary}")
    _bump("total", "since_heartbeat")
    started = time.monotonic()
    try:
        result = await awaitable
        _bump("ok")
        _log(f"{name} ok: {time.monotonic() - started:.1f}s")
        return result
    except Exception as exc:
        _bump("err")
        _log(f"{name} err: {type(exc).__name__} {exc} ({time.monotonic() - started:.1f}s)")
        raise


def _track_sync(name: str, summary: str, fn):
    _log(f"{name} start: {summary}")
    _bump("total", "since_heartbeat")
    started = time.monotonic()
    try:
        result = fn()
        _bump("ok")
        _log(f"{name} ok: {time.monotonic() - started:.1f}s")
        return result
    except Exception as exc:
        _bump("err")
        _log(f"{name} err: {type(exc).__name__} {exc} ({time.monotonic() - started:.1f}s)")
        raise


class KnowledgeMCP:
    """Model tools bound once to an admitted application and resource configuration."""

    def __init__(self, application: WikiApplication) -> None:
        require_knowledge_admission(application._admission)
        self.application = application

    async def wiki_search(self,
        query: str,
        phase: str | None = None,
        tags: list[str] | None = None,
        type: str | None = None,
        limit: int = 10,
        intent: str | None = None,
        progress: str | None = None,
        task_description: str | None = None,
        call_count: int = 0,
        seen_ids: list[str] | None = None,
        device_feedback: str | None = None,
    ) -> dict:
        """Legacy cann-ask v5.2 search; new local agents should use kcwiki search."""
        selected = config.load_config(self.application.binding.paths.config_path)
        return await _track_async(
            "wiki_search",
            f"query={query!r} phase={phase or progress or 'correctness'} call_count={call_count} limit={limit}",
            search_enveloped(
                query=query,
                phase=phase,
                tags=tags,
                type=type,
                limit=limit,
                intent=intent,
                progress=progress,
                task_description=task_description,
                call_count=call_count,
                seen_ids=seen_ids,
                device_feedback=device_feedback,
                loader=self.application.knowledge,
                mode=selected.search_mode,
                resolver=AliasResolver(selected.aliases_path),
            ),
        )

    def wiki_get_page(self, id: str) -> dict:
        """Fetch one active logical page or exact revision through the shared service."""
        return _track_sync("wiki_get_page", f"id={id!r}", lambda: self.application.get_page_legacy(id))

    def wiki_search_v1(self, request: dict) -> dict:
        """Thin remote adapter for the same KnowledgeQuery service used by kcwiki."""
        return _track_sync(
            "wiki_search_v1",
            f"operator={request.get('operator_id')!r} phase={request.get('phase')!r}",
            lambda: self.application.search(request),
        )

    def wiki_append_trajectory_delta(self, delta: dict) -> dict:
        """Thin remote adapter for the same idempotent outbox used by kcwiki."""
        return _track_sync(
            "wiki_append_trajectory_delta",
            f"delta={delta.get('trajectory_delta_id')!r} episode={delta.get('episode_id')!r}",
            lambda: self.application.append_trajectory(delta, legacy=True),
        )

    def wiki_grep_raw(self,
        pattern: str,
        scope: str | None = None,
        context: int = 4,
        glob: str | None = None,
        include_tests: bool = False,
        max_hits: int = 30,
    ) -> dict:
        """Bounded regex search over read-only raw evidence through the shared service."""
        return _track_sync(
            "wiki_grep_raw",
            f"pattern={pattern!r} scope={scope!r}",
            lambda: self.application.raw_grep(
                pattern=pattern,
                scope=scope,
                context=context,
                glob=glob,
                include_tests=include_tests,
                max_hits=max_hits,
                legacy=True,
            ),
        )

    def wiki_read_raw(self, path: str, start: int = 1, end: int | None = None) -> dict:
        """Read a bounded raw line range through the shared service."""
        return _track_sync(
            "wiki_read_raw",
            f"path={path!r} start={start} end={end}",
            lambda: self.application.raw_read(path=path, start=start, end=end, legacy=True),
        )

    def wiki_help(self) -> dict:
        """Return the CLI-first/MCP-compatible contract."""
        return _track_sync("wiki_help", "", help_payload)

    def wiki_get_index(self) -> dict:
        """[DEPRECATED] Prefer kcwiki search or wiki_search + wiki_get_page."""
        return _track_sync("wiki_get_index", "deprecated", self.application.page_reader.get_index)

    def wiki_submit_trajectory(self, session_id: str, content: str) -> dict:
        """Persist a legacy Markdown trace during migration."""
        return _track_sync(
            "wiki_submit_trajectory",
            f"session_id={session_id!r} bytes={len(content or '')}",
            lambda: save_trajectory(
                session_id,
                content,
                uploaded_dir=config.load_config(
                    self.application.binding.paths.config_path
                ).trajectory_uploaded_dir,
            ),
        )


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="AscendC Wiki MCP compatibility server")
    parser.add_argument("--expected-python-modules", type=Path)
    parser.add_argument("--startup-report", type=Path, required=True)
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "3000")))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--heartbeat-seconds", type=int, default=1800)
    return parser.parse_args(argv)


def _heartbeat_loop(interval: int) -> None:
    while True:
        time.sleep(interval)
        with _stats_lock:
            snapshot = dict(_stats)
            _stats["since_heartbeat"] = 0
        _log("heartbeat: " + " ".join(f"{key}={value}" for key, value in snapshot.items()))


def create_server(application: WikiApplication):
    """Load the explicitly installed optional transport only when starting it."""
    from importlib import import_module

    adapter = KnowledgeMCP(application)
    server = import_module("fastmcp").FastMCP("AscendC Wiki")
    for name in (
        "wiki_search", "wiki_get_page", "wiki_search_v1", "wiki_append_trajectory_delta",
        "wiki_grep_raw", "wiki_read_raw", "wiki_help", "wiki_get_index", "wiki_submit_trajectory",
    ):
        server.tool()(getattr(adapter, name))
    return server


def main(
    argv: list[str] | None = None, *, expected_modules: object | None = None,
) -> None:
    args = parse_args(argv)
    application = standalone_application(
        None, expected_modules=expected_modules, binding_path=args.expected_python_modules,
        startup_report=args.startup_report,
    )
    server = create_server(application)
    _log(f"server start: {args.host}:{args.port} mode={config.CONFIG.search_mode}")
    if args.heartbeat_seconds > 0:
        threading.Thread(
            target=_heartbeat_loop,
            args=(args.heartbeat_seconds,),
            daemon=True,
        ).start()
    server.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()

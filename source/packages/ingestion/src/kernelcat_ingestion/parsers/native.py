"""Lossless source spans plus bounded native structure; never execute input.

Python symbols are parsed using ast. C-family sources are lossless line spans,
not a claimed C++ AST or semantic dependency closure. Stored profiler results
remain source observations, never independent evaluation receipts.
"""
from __future__ import annotations

import ast
import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import yaml
from kernelcat_executor.contracts.models import canonical, digest

from kernelcat_ingestion.parsers.identity import RECIPE, interpretation, observe_environment
from kernelcat_ingestion.plans import Element, Parsed, Snapshot

CODE = {".py", ".cpp", ".cc", ".c", ".cu", ".h", ".hpp", ".cuh", ".mlir"}
TEXT = {".md", ".txt", ".rst", ".tex", ".diff", ".patch", ".cmake", ".log"}


def strict_json(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    value = json.loads(text, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    canonical(value)
    return value


class UniqueYAML(yaml.SafeLoader):
    pass


def _yaml_mapping(loader: UniqueYAML, node: yaml.MappingNode) -> dict[Any, Any]:
    pairs = loader.construct_pairs(node, deep=True)
    value: dict[Any, Any] = {}
    for key, item in pairs:
        if not isinstance(key, str) or key in value:
            raise ValueError("non-string or duplicate YAML key")
        value[key] = item
    return value


UniqueYAML.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _yaml_mapping)


@dataclass(frozen=True)
class NativeParser:
    """One observed environment shared by the finite runtime and its parses."""

    environment: str = field(default_factory=observe_environment, init=False)

    def parse(self, snapshot: Snapshot, raw: bytes, chunk_lines: int = 96) -> Parsed:
        if observe_environment() != self.environment:
            raise ValueError("parser environment changed; create a new parser")
        return _parse(snapshot, raw, chunk_lines, self.environment)


def parse(snapshot: Snapshot, raw: bytes, chunk_lines: int = 96) -> Parsed:
    return _parse(snapshot, raw, chunk_lines, observe_environment())


def _parse(snapshot: Snapshot, raw: bytes, chunk_lines: int, environment: str) -> Parsed:
    if type(chunk_lines) is not int or not 1 <= chunk_lines <= 1000:
        raise ValueError("invalid line chunk bound")
    identity = interpretation(environment, snapshot, chunk_lines)
    identity_digest = digest(identity)
    parser = RECIPE + ":" + canonical(identity)
    if len(raw) > 16777216:
        return Parsed(parser=parser, snapshot=snapshot, disposition="parse_failed", warnings=("input_limit",))
    items: list[Element] = []
    output_bytes = 0
    warnings: list[str] = []
    ext = PurePosixPath(snapshot.name).suffix.lower()

    def add(kind: Any, selector: dict[str, Any], text: str, **context: Any) -> None:
        nonlocal output_bytes
        if len(items) >= 4096:
            raise ValueError("element count limit")
        selector = {"artifact_ref": snapshot.artifact_ref, **selector}
        eid = "ev:data:" + digest({"source": snapshot.source_id, "selector": selector,
                                    "parser": identity_digest, "kind": kind})
        element = Element(evidence_id=eid, source_id=snapshot.source_id, kind=kind,
                          selector=selector, text=text, context={**context, "parser_identity": {
                              "recipe": RECIPE, "digest": identity_digest, "descriptor": identity}})
        output_bytes += len(canonical(element).encode())
        if output_bytes > 33554432:
            raise ValueError("parsed output limit")
        items.append(element)

    try:
        text = raw.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError("binary NUL in text")
        lines = text.splitlines(keepends=True)
        if not lines:
            return Parsed(parser=parser, snapshot=snapshot, disposition="needs_evidence", warnings=("empty_file",))
        if ext in {".json", ".yaml", ".yml"}:
            if ext == ".json":
                value = strict_json(text)
            else:
                if any(isinstance(token, (yaml.AliasToken, yaml.AnchorToken))
                       for token in yaml.scan(text)):
                    raise ValueError("YAML aliases are not supported in bounded ingestion")
                value = yaml.load(text, Loader=UniqueYAML)
                canonical(value)
            add("config", {"kind": "document"}, text, structure=value,
                authority="source_observation")
        elif ext in {".csv", ".tsv"}:
            reader = csv.reader(io.StringIO(text, newline=""),
                                delimiter="\t" if ext == ".tsv" else ",", strict=True)
            header = next(reader)
            if not header or any(not x for x in header) or len(set(header)) != len(header):
                raise ValueError("empty or duplicate table headers")
            records = []
            for index, row in enumerate(reader, 1):
                if index > 100000:
                    raise ValueError("table row count limit")
                if len(row) != len(header):
                    raise ValueError("ragged table row")
                records.append({"row": index, "values": row})
            add("table", {"kind": "document"}, text, header=header, rows=records,
                authority="source_observation", units="only_explicit_header_units")
        elif ext == ".ipynb":
            book = strict_json(text)
            if not isinstance(book, dict) or book.get("nbformat") != 4:
                raise ValueError("unsupported notebook format")
            cells = book.get("cells")
            if not isinstance(cells, list):
                raise ValueError("notebook cells must be an array")
            for index, cell in enumerate(cells):
                if not isinstance(cell, dict) or cell.get("cell_type") not in {"code", "markdown", "raw"}:
                    raise ValueError("invalid cell type")
                source = cell["source"]
                source = "".join(source) if isinstance(source, list) else source
                if not isinstance(source, str):
                    raise ValueError("invalid cell source")
                add("notebook_cell", {"kind": "cell", "index": index}, source,
                    cell_type=cell["cell_type"], cell_id=cell.get("id"),
                    outputs=cell.get("outputs", []), execution_count=cell.get("execution_count"),
                    authority="saved_output_not_reexecuted")
        elif ext in CODE | TEXT or PurePosixPath(snapshot.name).name == "CMakeLists.txt":
            symbols: list[dict[str, Any]] = []
            if ext == ".py":
                try:
                    tree = ast.parse(text)
                    symbols = [{"name": n.name, "start": min([n.lineno] +
                               [d.lineno for d in n.decorator_list]), "end": n.end_lineno}
                               for n in ast.walk(tree)
                               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
                    if len(symbols) > 8192:
                        raise ValueError("symbol count limit")
                except SyntaxError:
                    warnings.append("python_syntax_error_preserved")
            if ext in CODE - {".py"}:
                warnings.append("line_spans_not_semantic_dependency_closure")
            for start in range(0, len(lines), chunk_lines):
                end = min(start + chunk_lines, len(lines))
                kind = "code" if ext in CODE or ext in {".diff", ".patch"} else (
                    "log" if snapshot.role in {"log", "measurement"} else "text")
                add(kind, {"kind": "lines", "start": start + 1, "end": end},
                    "".join(lines[start:end]),
                    symbols=[symbol for symbol in symbols if symbol["start"] <= end and symbol["end"] > start],
                    source_context=snapshot.artifact_ref, authority="source_observation")
        else:
            return Parsed(parser=parser, snapshot=snapshot, disposition="partial", warnings=("unsupported_format",))
    except (UnicodeError, ValueError, TypeError, KeyError, csv.Error, yaml.YAMLError,
            RecursionError) as error:
        return Parsed(parser=parser, snapshot=snapshot, disposition="parse_failed",
                      warnings=(type(error).__name__,))
    return Parsed(parser=parser, snapshot=snapshot, disposition="accepted", elements=tuple(items),
                  warnings=tuple(warnings))

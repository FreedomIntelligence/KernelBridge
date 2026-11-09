"""Read-only, content-pinned SKILL.md catalogue and progressive resource loading."""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from kernelcat_executor.contracts.models import Json, ToolFailure, digest
from kernelcat_executor.contracts.text import text_page


class Skills:
    def __init__(self, roots: tuple[Path, ...], max_bytes: int = 131072) -> None:
        self.max_bytes = max_bytes
        self.items: dict[str, tuple[Path, Json]] = {}
        self.manifests: dict[str, dict[str, str]] = {}
        self.aliases: dict[str, dict[str, str]] = {}
        for root in roots:
            root = root.resolve(strict=True)
            for path in sorted(root.glob("*/SKILL.md")):
                if not path.resolve().is_relative_to(root):
                    raise ToolFailure("skill symlink escapes its configured root")
                text = self._read(path)
                metadata: Json = {}
                if text.startswith("---\n"):
                    header, separator, _ = text[4:].partition("\n---")
                    if not separator:
                        raise ToolFailure("unclosed skill frontmatter")
                    try:
                        metadata = yaml.safe_load(header)
                    except yaml.YAMLError:
                        raise ToolFailure("invalid skill YAML") from None
                if not isinstance(metadata, dict):
                    raise ToolFailure(f"invalid skill frontmatter: {path.name}")
                name = metadata.get("name", path.parent.name)
                if not isinstance(name, str) or not name:
                    raise ToolFailure("skill name must be a nonempty string")
                manifest = self._manifest(path.parent)
                item = {"name": name, "description": str(metadata.get("description", "")),
                        "revision": digest(manifest)}
                if name in self.items:
                    if self.manifests[name] != manifest:
                        raise ToolFailure(f"conflicting skill definitions or resources: {name}")
                    continue
                self.items[name] = (path, item)
                self.manifests[name] = manifest
                options = metadata.get("metadata", {})
                if not isinstance(options, dict):
                    raise ToolFailure("skill metadata must be a mapping")
                if "alias-of" in options:
                    allowed = {"alias-of", "route-hint", "resource-prefix"}
                    alias = {key: options[key] for key in allowed if key in options}
                    if not all(isinstance(value, str) and value for value in alias.values()):
                        raise ToolFailure("alias fields must be nonempty strings")
                    self.aliases[name] = alias
        # Validate only after all explicitly configured roots have been discovered.
        # Do not follow alias chains or grant filesystem access outside those roots.
        for name, options in self.aliases.items():
            target = options["alias-of"]
            if target == name or target not in self.items or target in self.aliases:
                raise ToolFailure("alias must target an existing canonical skill, without chains")
            for key in ("route-hint", "resource-prefix"):
                if key in options:
                    relative = Path(options[key])
                    if relative.is_absolute() or ".." in relative.parts:
                        raise ToolFailure("alias resources must remain inside the canonical skill")
                    root = self.items[target][0].parent.resolve()
                    resource = (root / relative).resolve(strict=True)
                    if not resource.is_relative_to(root):
                        raise ToolFailure("alias resource escapes its canonical skill")
                    if key == "route-hint" and not resource.is_file():
                        raise ToolFailure("alias route hint must be a file")
                    if key == "resource-prefix" and not resource.is_dir():
                        raise ToolFailure("alias resource prefix must be a directory")

    @staticmethod
    def _manifest(root: Path) -> dict[str, str]:
        manifest = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ToolFailure("skill resource symlinks are forbidden")
            if path.is_file():
                with path.open("rb") as stream:
                    manifest[str(path.relative_to(root))] = hashlib.file_digest(stream, "sha256").hexdigest()
        return manifest

    def fingerprint(self) -> str:
        return digest(self.manifests)

    def _read(self, path: Path) -> str:
        if path.stat().st_size > self.max_bytes:
            raise ToolFailure("skill resource exceeds configured size limit")
        return path.read_bytes().decode("utf-8")

    def catalog(self) -> list[Json]:
        return [dict(meta) for name, (_, meta) in self.items.items() if name not in self.aliases]

    def load(self, name: str, resource: str = "SKILL.md", offset: int = 0, limit: int = 8192) -> Json:
        if name not in self.items:
            raise ToolFailure("unknown skill")
        requested_name = name
        alias = self.aliases.get(name)
        if alias is not None:
            # Verify the redirect definition too; it participates in campaign identity.
            if self._manifest(self.items[name][0].parent) != self.manifests[name]:
                raise ToolFailure("skill alias changed since catalogue creation")
            name = alias["alias-of"]
            if resource != "SKILL.md" and "resource-prefix" in alias:
                requested = Path(resource)
                if requested.is_absolute() or ".." in requested.parts:
                    raise ToolFailure("skill resource must be relative without traversal")
                prefix = Path(alias["resource-prefix"])
                # Returned page paths are canonical; remapping them must be idempotent.
                if (requested.parts and requested.parts[0] == "references"
                        and not requested.is_relative_to(prefix)):
                    resource = str(prefix.joinpath(*requested.parts[1:]))
        root = self.items[name][0].parent.resolve()
        raw = Path(resource)
        if raw.is_absolute() or ".." in raw.parts:
            raise ToolFailure("skill resource must be a relative path without traversal")
        current = root
        for part in raw.parts:
            current /= part
            if current.is_symlink():
                raise ToolFailure("skill resource symlinks are forbidden")
        path = current.resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ToolFailure("resource escapes skill directory")
        text = self._read(path)
        sha = hashlib.sha256(text.encode()).hexdigest()
        if self.manifests[name].get(str(path.relative_to(root))) != sha:
            raise ToolFailure("skill resource changed since catalogue creation; start a new campaign")
        try:
            result = {"name": name, "resource": resource, **text_page(text, offset, limit)}
            if alias is not None:
                result.update(requested_name=requested_name, alias=True,
                              route_hint=alias.get("route-hint"))
            return result
        except ValueError as error:
            raise ToolFailure(str(error)) from None

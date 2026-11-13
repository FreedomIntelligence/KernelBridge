"""Export explicitly selected public SDK text into a portable, hash-bound snapshot.

This is a host provisioning utility, never an agent tool. It does not execute SDK
code, contact a network endpoint, compile, or initialize a device.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat


SCHEMA = 'kernelbridge.sdk-snapshot/v1'
DEFAULT_EXTENSIONS = ('.h', '.hpp', '.hxx', '.c', '.cpp', '.cc', '.cxx', '.cuh',
                      '.inc', '.inl', '.txt', '.md', '.rst', '.json', '.yaml',
                      '.yml', '.cmake', '.py', '.sh')
DEFAULT_FILENAMES = ('CMakeLists.txt',)


class SnapshotError(ValueError):
    """An incomplete or changing source cannot become a usable snapshot."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def _text(value, field):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(
            ord(char) < 32 or ord(char) == 127 for char in value):
        raise SnapshotError('Invalid ' + field)
    return value


def _relative(value):
    _text(value, 'relative path')
    path = PurePosixPath(value)
    if ('\\' in value or path.is_absolute() or value != path.as_posix()
            or any(part in ('', '.', '..') for part in value.split('/'))):
        raise SnapshotError('Expected a normalized relative path: ' + value)
    return value


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _resolved(root, logical):
    path = root / logical
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SnapshotError('Unreadable or cyclic SDK path: ' + logical) from error
    if not resolved.is_relative_to(root):
        raise SnapshotError('SDK path escapes canonical root: ' + logical)
    return resolved


def _scan(root, includes, extensions, max_entries):
    """Keep logical aliases; reject directory cycles and every escaping link."""
    files, excluded, structure = [], [], []
    entries = 0

    def visit(logical, ancestors, links):
        nonlocal entries
        entries += 1
        if entries > max_entries:
            raise SnapshotError('SDK inventory exceeds max_inventory_entries')
        original = root / logical
        before = original.lstat()
        resolved = _resolved(root, logical)
        source = resolved.stat()
        current_links = list(links)
        if stat.S_ISLNK(before.st_mode):
            current_links.append({'path': logical, 'target': os.readlink(original),
                                  'resolved_path': str(resolved)})
        if _signature(original.lstat()) != _signature(before):
            raise SnapshotError('SDK path changed during inventory: ' + logical)
        structure.append((logical, str(resolved), _signature(before), _signature(source)))
        if stat.S_ISDIR(source.st_mode):
            identity = (source.st_dev, source.st_ino)
            if identity in ancestors:
                raise SnapshotError('Cyclic SDK directory alias: ' + logical)
            for name in sorted(os.listdir(resolved)):
                _relative(name)
                visit(logical + '/' + name, ancestors | {identity}, current_links)
            return
        if not stat.S_ISREG(source.st_mode):
            raise SnapshotError('SDK inventory contains a nonregular entry: ' + logical)
        selected = (PurePosixPath(logical).suffix.lower() in extensions
                    or PurePosixPath(logical).name in DEFAULT_FILENAMES)
        item = {'path': logical, 'source_path': str(resolved),
                'source_links': current_links, '_signature': _signature(source)}
        if selected:
            files.append(item)
        else:
            excluded.append({'path': logical, 'bytes': source.st_size,
                             'reason': 'extension_not_selected'})

    for include in includes:
        resolved = _resolved(root, include)
        if not resolved.is_dir():
            raise SnapshotError('Public include root must be a directory: ' + include)
        # Include roots may themselves be nested below symlinks. Record every
        # such component, not only the last component visited by the walker.
        inherited = []
        parts = PurePosixPath(include).parts
        for end in range(1, len(parts)):
            prefix = '/'.join(parts[:end])
            source = root / prefix
            if source.is_symlink():
                inherited.append({'path': prefix, 'target': os.readlink(source),
                                  'resolved_path': str(_resolved(root, prefix))})
        visit(include, set(), inherited)
    return sorted(files, key=lambda row: row['path']), sorted(excluded, key=lambda row: row['path']), structure


def _read_source(root, item, limit):
    """Open canonical components without following replacement symlinks."""
    logical = item['path']
    resolved = _resolved(root, logical)
    if str(resolved) != item['source_path']:
        raise SnapshotError('SDK link target changed: ' + logical)
    relative = resolved.relative_to(root)
    nofollow = getattr(os, 'O_NOFOLLOW', None)
    directory = getattr(os, 'O_DIRECTORY', None)
    if nofollow is None or directory is None or os.open not in os.supports_dir_fd:
        raise SnapshotError('Snapshot export requires no-follow directory descriptors')
    fd = os.open(root, os.O_RDONLY | directory | nofollow)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | directory | nofollow, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        leaf = os.open(relative.name, os.O_RDONLY | nofollow | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(leaf)
            if not stat.S_ISREG(before.st_mode) or _signature(before) != item['_signature']:
                raise SnapshotError('SDK file identity changed: ' + logical)
            if before.st_size > limit:
                raise SnapshotError('SDK text file exceeds max_file_bytes: ' + logical)
            with os.fdopen(leaf, 'rb', closefd=False) as stream:
                raw = stream.read(limit + 1)
            if len(raw) > limit or _signature(os.fstat(leaf)) != _signature(before):
                raise SnapshotError('SDK file changed during read: ' + logical)
        finally:
            os.close(leaf)
    finally:
        os.close(fd)
    if _resolved(root, logical) != resolved or _signature(resolved.stat()) != item['_signature']:
        raise SnapshotError('SDK file changed after read: ' + logical)
    if b'\x00' in raw:
        raise SnapshotError('Selected SDK text contains NUL: ' + logical)
    # Public source may contain comments in an undocumented encoding. Preserve
    # its original bytes; neither guess a codec nor transcode SDK declarations.
    return raw


def _write_new(path, raw):
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o400)


def export_snapshot(root, output, *, name, version, target, include,
                    extensions=None, max_files=50000, max_bytes=536870912,
                    max_file_bytes=8388608, origin=None):
    """Export all selected text or fail without publishing ``manifest.json``.

    Limits are explicit provisioning bounds, not an optimization/token budget.
    The returned manifest identifies exactly the exported public source set;
    it is not evidence of compilation, candidate correctness or performance.
    """
    for field, value in [('name', name), ('version', version), ('target', target)]:
        _text(value, field)
        if len(value) > 1024:
            raise SnapshotError(field + ' exceeds 1024 characters')
    if not isinstance(include, (list, tuple)) or not include:
        raise SnapshotError('include must explicitly name public SDK subdirectories')
    includes = sorted(_relative(value) for value in include)
    if len(set(includes)) != len(includes) or any(
            right.startswith(left + '/') for index, left in enumerate(includes)
            for right in includes[index + 1:]):
        raise SnapshotError('Duplicate or overlapping logical include directories')
    for field, value, ceiling in [('max_files', max_files, 50000),
                                  ('max_bytes', max_bytes, 536870912),
                                  ('max_file_bytes', max_file_bytes, 8388608)]:
        if type(value) is not int or not 1 <= value <= ceiling:
            raise SnapshotError('Invalid ' + field)
    extensions = DEFAULT_EXTENSIONS if extensions is None else extensions
    if not isinstance(extensions, (list, tuple)) or not extensions:
        raise SnapshotError('extensions must be a nonempty list')
    if any(not isinstance(ext, str) or len(ext) > 16 or not ext.startswith('.')
           or not ext[1:].isascii() or not ext[1:].isalnum() or ext != ext.lower()
           for ext in extensions):
        raise SnapshotError('Invalid text extension')
    extensions = sorted(set(extensions))
    source_root = Path(root).absolute()
    root = source_root.resolve(strict=True)
    if not root.is_dir():
        raise SnapshotError('SDK root must be a directory')
    requested = Path(output).absolute()
    output = requested.parent.resolve(strict=True) / requested.name
    if output.is_relative_to(root):
        raise SnapshotError('Snapshot output must be outside the source SDK')
    if origin is None:
        origin = {}
    if not isinstance(origin, dict) or {'source_root', 'canonical_root'} & set(origin):
        raise SnapshotError('origin must be an object without reserved root fields')
    # Validate metadata before creating output. Caller facts remain provenance;
    # no metadata value becomes an executable command or a filesystem path.
    complete_origin = {'source_root': str(source_root), 'canonical_root': str(root), **origin}
    if len(canonical(complete_origin)) > 8192:
        raise SnapshotError('origin provenance exceeds 8192 bytes')
    output.mkdir(mode=0o700, exist_ok=False)
    output_signature = _signature(output.stat())[:2]
    (output / 'files').mkdir(mode=0o700)
    created_at = datetime.now(timezone.utc).isoformat()
    try:
        max_entries = max(10000, max_files * 10)
        files, excluded, structure = _scan(root, includes, extensions, max_entries)
        if not files:
            raise SnapshotError('Selected public directories contain no supported text')
        if len(files) > max_files:
            raise SnapshotError('SDK text inventory exceeds max_files')
        result, total = [], 0
        for item in files:
            raw = _read_source(root, item, max_file_bytes)
            total += len(raw)
            if total > max_bytes:
                raise SnapshotError('SDK snapshot exceeds max_bytes')
            destination = output / 'files' / item['path']
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if destination.parent.resolve() != destination.parent:
                raise SnapshotError('Snapshot output directory became linked')
            _write_new(destination, raw)
            result.append({'path': item['path'], 'bytes': len(raw), 'sha256': sha(raw),
                           'source_path': item['source_path'],
                           'source_links': item['source_links']})
        # Re-inventory names/links/identities, then re-hash selected files before
        # publishing. A changed SDK cannot silently borrow this snapshot ID.
        _, final_excluded, final_structure = _scan(root, includes, extensions, max_entries)
        if structure != final_structure or excluded != final_excluded:
            raise SnapshotError('SDK inventory changed during export')
        for item, exported in zip(files, result):
            if sha(_read_source(root, item, max_file_bytes)) != exported['sha256']:
                raise SnapshotError('SDK content changed during export: ' + item['path'])
        if output.resolve() != output or _signature(output.stat())[:2] != output_signature:
            raise SnapshotError('Snapshot output identity changed')
        payload = {'schema_version': SCHEMA, 'name': name, 'version': version,
                   'target': target,
                   'origin': complete_origin,
                   'export': {'include': includes, 'extensions': extensions,
                              'filenames': list(DEFAULT_FILENAMES),
                              'max_files': max_files, 'max_bytes': max_bytes,
                              'max_file_bytes': max_file_bytes,
                              'content_policy': 'nul_free_original_bytes; encoding_not_inferred',
                              'max_inventory_entries': max_entries,
                              'files': len(result), 'bytes': total,
                              'excluded': excluded,
                              'source_revalidated_before_publish': True},
                   'files': result}
        manifest = {**payload, 'snapshot_id': sha(canonical(payload))}
        raw_manifest = canonical(manifest)
        if len(raw_manifest) > 33554432:
            raise SnapshotError('SDK snapshot manifest exceeds 32 MiB')
        # Non-model consumers may still archive/copy the snapshot normally.
        # Read-only modes also prevent accidental edits outside the handler.
        for folder, _, _ in os.walk(output / 'files', topdown=False):
            Path(folder).chmod(0o500)
        # The publication name appears only after its complete bytes are synced.
        # Keeping the private staging inode avoids any fallible cleanup after
        # publication. It is not an entry in the SDK corpus.
        _write_new(output / 'manifest.pending', raw_manifest)
        os.link(output / 'manifest.pending', output / 'manifest.json')
        return manifest
    except BaseException as error:
        # No valid manifest is exposed on a partial export. Keep partial bytes
        # and a bounded failure record for inspection, never reuse this output.
        if not (output / 'manifest.json').exists():
            status = {'status': 'failed', 'created_at': created_at,
                      'error_type': type(error).__name__, 'reason': str(error)[:2000]}
            try:
                _write_new(output / 'export-failure.json', canonical(status))
            except OSError:
                pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--include', action='append', required=True,
                        help='Explicit public subdirectory relative to root; repeatable')
    parser.add_argument('--extension', action='append', dest='extensions')
    parser.add_argument('--max-files', type=int, default=50000)
    parser.add_argument('--max-bytes', type=int, default=536870912)
    parser.add_argument('--max-file-bytes', type=int, default=8388608)
    parser.add_argument('--origin-json', type=Path,
                        help='Optional JSON object of provisioning provenance facts')
    args = parser.parse_args()
    origin = json.loads(args.origin_json.read_text()) if args.origin_json else None
    manifest = export_snapshot(args.root, args.output, name=args.name,
                               version=args.version, target=args.target,
                               include=args.include, extensions=args.extensions,
                               max_files=args.max_files, max_bytes=args.max_bytes,
                               max_file_bytes=args.max_file_bytes, origin=origin)
    print(json.dumps({'manifest': str(args.output.absolute() / 'manifest.json'),
                      'snapshot_id': manifest['snapshot_id'],
                      'files': len(manifest['files']), 'bytes': manifest['export']['bytes']},
                     ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()

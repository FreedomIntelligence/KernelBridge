"""Pack the editable native sources into the six-source evaluator format; never execute them."""
import argparse
import hashlib
import json
from pathlib import Path

FIELDS = ('project_json_src', 'host_tiling_src', 'host_operator_src', 'kernel_src', 'python_bind_src', 'model_src')


def pack(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory / 'sources.json').read_text())
    if set(manifest['fields']) != set(FIELDS):
        raise ValueError('exactly six source fields are required')
    result = ['"""Native CANN sources packed by pack_native.py; edit native/ and repack."""\n']
    for field in FIELDS:
        relative = Path(manifest['fields'][field]['path'])
        path = directory / relative
        if relative.is_absolute() or '..' in relative.parts or path.is_symlink() or path.resolve() != path or not path.is_file():
            raise ValueError('source must be a regular file inside native/')
        if path.stat().st_size > 4_000_000:
            raise ValueError('source exceeds 4 MB')
        text = path.read_text()
        if not text.strip():
            raise ValueError('empty source: ' + field)
        if field == 'project_json_src':
            json.loads(text)
        result.append(field + ' = ' + repr(text) + '\n')
    return '\n'.join(result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    text = pack(args.native)
    # Explicit --output is the caller's choice; only write this one wrapper.
    args.output.write_text(text)
    print(json.dumps({'output': str(args.output.resolve()), 'sha256': hashlib.sha256(text.encode()).hexdigest()}))


if __name__ == '__main__':
    main()

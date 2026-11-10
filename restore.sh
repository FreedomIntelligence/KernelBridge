#!/bin/sh
# Run from any directory, including paths containing spaces.
set -eu
KB_RESTORE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if [ -n "${KERNELBRIDGE_BOOTSTRAP_PYTHON:-}" ]; then
    exec "$KERNELBRIDGE_BOOTSTRAP_PYTHON" -I -B "$KB_RESTORE_ROOT/restore.py" "$@"
fi
for KB_RESTORE_PYTHON in python3 python3.12 python3.11 python3.13; do
    if command -v "$KB_RESTORE_PYTHON" >/dev/null 2>&1 && "$KB_RESTORE_PYTHON" -I -B -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        exec "$KB_RESTORE_PYTHON" -I -B "$KB_RESTORE_ROOT/restore.py" "$@"
    fi
done
printf '%s\n' 'Install Python 3.11+ with venv support, or set KERNELBRIDGE_BOOTSTRAP_PYTHON to its absolute path.' >&2
exit 2

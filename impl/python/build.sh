#!/usr/bin/env bash
# Build/install the Python mysql-connector-python driver (server-side prepared
# statements). Idempotent. The connector ships as a self-contained wheel, so no
# system headers or compiler are needed — only python3 + venv + network.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"

# Already satisfied by a previously built venv?
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import mysql.connector" >/dev/null 2>&1; then
    echo "mysql-connector-python available in $VENV" >&2
    exit 0
fi

python3 -m venv "$VENV" || {
    echo "error: failed to create venv (install python3-venv)" >&2
    exit 1
}
if ! "$VENV/bin/pip" install --quiet --disable-pip-version-check mysql-connector-python >&2; then
    echo "error: pip install mysql-connector-python failed — check network access to PyPI" >&2
    exit 1
fi
"$VENV/bin/python" -c "import mysql.connector" >/dev/null 2>&1 || {
    echo "error: mysql-connector-python installed but import failed" >&2
    exit 1
}
echo "mysql-connector-python installed into $VENV" >&2

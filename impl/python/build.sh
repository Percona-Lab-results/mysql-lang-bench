#!/usr/bin/env bash
# Build/install the Python mysqlclient driver. Idempotent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"

# Already satisfied by a previously built venv?
if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import MySQLdb" >/dev/null 2>&1; then
    echo "mysqlclient available in $VENV" >&2
    exit 0
fi

# Satisfied by the system python3 (e.g. python3-mysqldb package)?
if python3 -c "import MySQLdb" >/dev/null 2>&1; then
    echo "mysqlclient available in system python3" >&2
    exit 0
fi

# Otherwise create a venv and pip-install mysqlclient.
python3 -m venv "$VENV" || {
    echo "error: failed to create venv (install python3-venv)" >&2
    exit 1
}
if ! "$VENV/bin/pip" install mysqlclient >&2; then
    rm -rf "$VENV"
    echo "error: pip install mysqlclient failed — install libmysqlclient-dev and python3-dev (or the system package python3-mysqldb)" >&2
    exit 1
fi
"$VENV/bin/python" -c "import MySQLdb" >/dev/null 2>&1 || {
    echo "error: mysqlclient installed but import failed — install libmysqlclient-dev and python3-dev (or the system package python3-mysqldb)" >&2
    exit 1
}
echo "mysqlclient installed into $VENV" >&2

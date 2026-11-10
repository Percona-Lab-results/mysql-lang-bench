#!/usr/bin/env bash
# Install the mysql2 driver for the JavaScript benchmark. Idempotent.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v node >/dev/null 2>&1 || {
    echo "error: node not found — apt install nodejs npm" >&2
    exit 1
}
command -v npm >/dev/null 2>&1 || {
    echo "error: npm not found — apt install nodejs npm" >&2
    exit 1
}

cd "$SCRIPT_DIR"

if [ -d node_modules/mysql2 ]; then
    echo "mysql2 available in $SCRIPT_DIR/node_modules" >&2
    exit 0
fi

npm install --no-audit --no-fund >&2 || {
    echo "error: npm install failed — check network access to the npm registry" >&2
    exit 1
}
[ -d node_modules/mysql2 ] || {
    echo "error: npm install succeeded but node_modules/mysql2 is missing" >&2
    exit 1
}
echo "mysql2 installed into $SCRIPT_DIR/node_modules" >&2

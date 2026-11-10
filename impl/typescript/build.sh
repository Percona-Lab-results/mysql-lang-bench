#!/usr/bin/env bash
# Build the TypeScript benchmark: install deps (mysql2 + typescript) and
# compile bench.ts into dist/ with tsc. Idempotent.
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

if [ ! -d node_modules/typescript ] || [ ! -d node_modules/mysql2 ]; then
    npm install --no-audit --no-fund >&2 || {
        echo "error: npm install failed — check network access to the npm registry" >&2
        exit 1
    }
fi
[ -d node_modules/mysql2 ] || {
    echo "error: npm install succeeded but node_modules/mysql2 is missing" >&2
    exit 1
}
[ -d node_modules/typescript ] || {
    echo "error: npm install succeeded but node_modules/typescript is missing" >&2
    exit 1
}

npx tsc >&2 || {
    echo "error: tsc compilation failed" >&2
    exit 1
}
[ -f dist/bench.js ] || {
    echo "error: tsc succeeded but dist/bench.js is missing" >&2
    exit 1
}
echo "compiled bench.ts into $SCRIPT_DIR/dist" >&2

#!/usr/bin/env bash
# Build the C benchmark. Idempotent. Works when invoked from the repo root.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v mysql_config >/dev/null 2>&1; then
    echo "error: mysql_config not found — install libmysqlclient-dev (Debian/Ubuntu) or mysql-devel (RHEL/Fedora)" >&2
    exit 1
fi

cc bench.c -o bench $(mysql_config --cflags --libs) -O2 -pthread

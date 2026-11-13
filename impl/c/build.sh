#!/usr/bin/env bash
# Build the C benchmark. Idempotent. Works when invoked from the repo root.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v mysql_config >/dev/null 2>&1; then
    echo "error: mysql_config not found — install libmysqlclient-dev (Debian/Ubuntu) or mysql-devel (RHEL/Fedora)" >&2
    exit 1
fi

if ! printf '#include <openssl/md5.h>\nint main(void){return 0;}\n' \
        | cc -x c - -lcrypto -o /dev/null >/dev/null 2>&1; then
    echo "error: OpenSSL dev files (libcrypto) not found — install libssl-dev (Debian/Ubuntu) or openssl-devel (RHEL/Fedora)" >&2
    exit 1
fi

cc bench.c -o bench $(mysql_config --cflags --libs) -lcrypto -O2 -pthread

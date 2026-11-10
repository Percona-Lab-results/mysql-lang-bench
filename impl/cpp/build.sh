#!/usr/bin/env bash
# Build the C++ benchmark. Idempotent; run as: bash impl/cpp/build.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v mysql_config >/dev/null 2>&1; then
    echo "error: mysql_config not found — install libmysqlclient-dev (Debian/Ubuntu) or mysql-devel (RHEL)" >&2
    exit 1
fi

# shellcheck disable=SC2046  # word splitting of mysql_config output is intended
c++ -std=c++17 -O2 -pthread bench.cpp -o bench $(mysql_config --cflags --libs)

#!/usr/bin/env bash
# Build the Go benchmark. Invoked from the repo root (bash impl/go/build.sh),
# so all paths are computed relative to this script's own directory.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v go >/dev/null 2>&1; then
    echo "go not found: install the Go toolchain (e.g. 'apt install golang-go' or 'dnf install golang')" >&2
    exit 1
fi

cd "$DIR"

# First build fetches the pinned go-sql-driver/mysql and writes go.sum
# (needs network access to proxy.golang.org).
if [ ! -f go.sum ]; then
    go mod tidy
fi

go build -o bench .

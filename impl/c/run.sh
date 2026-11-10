#!/usr/bin/env bash
# Run the C benchmark. Config comes from environment variables (see SPEC.md).
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -x ./bench ]]; then
    echo "error: impl/c/bench not built — run 'bash impl/c/build.sh' first" >&2
    exit 1
fi

exec ./bench

#!/usr/bin/env bash
# Run the Ruby benchmark. Config comes from environment variables (SPEC.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v ruby >/dev/null 2>&1; then
    echo "error: ruby not found — install with: apt install ruby-full ruby-dev" >&2
    exit 1
fi

if ! ruby -e "require %q(mysql2)" >/dev/null 2>&1; then
    echo "error: mysql2 gem not available — run: bash impl/ruby/build.sh" >&2
    exit 1
fi

exec ruby "$SCRIPT_DIR/bench.rb"

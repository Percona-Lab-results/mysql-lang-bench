#!/usr/bin/env bash
# Run the Rust benchmark. All config comes from environment variables (see SPEC.md).
set -euo pipefail

cd "$(dirname "$0")"

BIN=target/release/mysql-bench-rust
if [[ ! -x "$BIN" ]]; then
    echo "rust benchmark binary not found ($PWD/$BIN): run 'bash impl/rust/build.sh' first" >&2
    exit 1
fi

exec "$BIN"

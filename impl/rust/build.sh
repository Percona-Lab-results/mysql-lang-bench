#!/usr/bin/env bash
# Build the Rust benchmark (release). Idempotent.
set -euo pipefail

cd "$(dirname "$0")"

# rustup installs to ~/.cargo/bin, which is not on PATH in non-interactive ssh
[ -d "$HOME/.cargo/bin" ] && export PATH="$HOME/.cargo/bin:$PATH"

if ! command -v cargo >/dev/null 2>&1; then
    echo "cargo not found: install Rust via rustup (curl https://sh.rustup.rs -sSf | sh) and re-run" >&2
    exit 1
fi

cargo build --release

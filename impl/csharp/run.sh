#!/usr/bin/env bash
# Run the C# benchmark. Invoked from the repo root (bash impl/csharp/run.sh).
# Config comes from env vars only; one JSON result line on stdout.
set -euo pipefail

export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/bin/Release/net8.0/bench"

if [ ! -x "$BIN" ]; then
    echo "benchmark binary not found at $BIN: run 'bash impl/csharp/build.sh' first" >&2
    exit 1
fi

exec "$BIN"

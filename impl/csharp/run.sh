#!/usr/bin/env bash
# Run the C# benchmark. Invoked from the repo root (bash impl/csharp/run.sh).
# Config comes from env vars only; one JSON result line on stdout.
set -euo pipefail

export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1

# Stock vs tuned split is the GC mode, chosen at runtime (the csproj does not
# pin it). BENCH_TUNED=1 -> server GC (tuned); otherwise workstation GC (stock).
if [ "${BENCH_TUNED:-0}" = "1" ]; then
    export DOTNET_gcServer=1
else
    export DOTNET_gcServer=0
fi

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$DIR/bin/Release/net8.0/bench"

if [ ! -x "$BIN" ]; then
    echo "benchmark binary not found at $BIN: run 'bash impl/csharp/build.sh' first" >&2
    exit 1
fi

exec "$BIN"

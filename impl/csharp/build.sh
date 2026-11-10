#!/usr/bin/env bash
# Build the C# benchmark. Invoked from the repo root (bash impl/csharp/build.sh),
# so all paths are computed relative to this script's own directory. Idempotent.
# NuGet restore (MySqlConnector) happens implicitly during `dotnet build`.
set -euo pipefail

export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v dotnet >/dev/null 2>&1; then
    echo "dotnet not found: install the .NET 8 SDK (e.g. 'apt install dotnet-sdk-8.0')" >&2
    exit 1
fi

cd "$DIR"
dotnet build -c Release -v q

#!/usr/bin/env bash
# Run the Java benchmark. Invoked from the repo root (bash impl/java/run.sh),
# so the classpath is computed relative to this script's own directory.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

exec java -cp "$DIR:$DIR/lib/mysql-connector-j.jar" Bench

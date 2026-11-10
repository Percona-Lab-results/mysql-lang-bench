#!/usr/bin/env bash
# Run the PHP benchmark. Config comes from environment variables (see SPEC.md).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec php "$SCRIPT_DIR/bench.php"

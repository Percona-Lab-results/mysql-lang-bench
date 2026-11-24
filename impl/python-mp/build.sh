#!/usr/bin/env bash
# Multi-process Python variant: same driver (mysqlclient) and interpreter as
# impl/python, so reuse its build (venv with mysqlclient, or system python3).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/../python/build.sh"

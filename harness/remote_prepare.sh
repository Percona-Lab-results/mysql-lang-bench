#!/usr/bin/env bash
# Runs ON the remote host. Recreates and reloads the sbtest table so every run
# starts from identical data and a comparable buffer-pool state.
set -euo pipefail
cd "$(dirname "$0")/.."
source config/mysql.env
source common/workload.env
export MYSQL_PWD="$MYSQL_PASSWORD"
MYSQL=("${MYSQL_CLI:-mysql}" --no-defaults --protocol=TCP -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$MYSQL_USER" "$MYSQL_DB")
"${MYSQL[@]}" < common/schema.sql
python3 common/load_data.py "$BENCH_TABLE_ROWS" | "${MYSQL[@]}"
echo "table reloaded: $BENCH_TABLE_ROWS rows"

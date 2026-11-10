#!/usr/bin/env bash
# Build step for the PHP implementation: nothing to compile, just verify
# that the PHP CLI and the mysqli extension are available.
set -euo pipefail

if ! command -v php >/dev/null 2>&1; then
    echo "php not found: install the PHP CLI and mysqli driver (e.g. apt install php-cli php-mysql)" >&2
    exit 1
fi

if ! php -m | grep -qix 'mysqli'; then
    echo "PHP mysqli extension missing: install the MySQL driver (e.g. apt install php-mysql)" >&2
    exit 1
fi

if ! php -m | grep -qix 'pcntl'; then
    echo "PHP pcntl extension missing: needed for forked workers (e.g. apt install php-cli / enable pcntl)" >&2
    exit 1
fi

exit 0

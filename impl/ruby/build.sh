#!/usr/bin/env bash
# Build/install the Ruby mysql2 driver (libmysqlclient C extension). Idempotent.
set -euo pipefail

if ! command -v ruby >/dev/null 2>&1 || ! command -v gem >/dev/null 2>&1; then
    echo "error: ruby/gem not found — install with: apt install ruby-full ruby-dev" >&2
    exit 1
fi

if ! command -v mysql_config >/dev/null 2>&1; then
    echo "error: mysql_config not found — install with: apt install libmysqlclient-dev" >&2
    exit 1
fi

# Already satisfied (system gem or a previous --user-install)?
if ruby -e "require %q(mysql2)" >/dev/null 2>&1; then
    echo "mysql2 gem already available" >&2
    exit 0
fi

# User install: no sudo needed; ~/.gem is on the default gem load path.
if ! gem install --user-install --no-document mysql2 >&2; then
    echo "error: gem install mysql2 failed — check that ruby-dev and libmysqlclient-dev are installed" >&2
    exit 1
fi

if ! ruby -e "require %q(mysql2)" >&2; then
    echo "error: mysql2 installed but require failed — gem environment:" >&2
    gem env >&2
    exit 1
fi

echo "mysql2 gem installed (user install)" >&2

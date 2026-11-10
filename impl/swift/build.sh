#!/usr/bin/env bash
# Build the Swift benchmark (release). Idempotent. Works when invoked from the repo root.
set -euo pipefail

cd "$(dirname "$0")"

# The Swift toolchain is not on PATH in non-interactive ssh; bench hosts keep it in ~/swift.
if command -v swift >/dev/null 2>&1; then
    SWIFT=swift
elif [[ -x "$HOME/swift/usr/bin/swift" ]]; then
    SWIFT="$HOME/swift/usr/bin/swift"
else
    echo "error: swift not found — install the Swift toolchain to ~/swift (expected $HOME/swift/usr/bin/swift)" >&2
    exit 1
fi

# Headers/lib come from Ubuntu's libmysqlclient-dev (no mysqlclient.pc, so we
# only sanity-check mysql_config; the module map uses the absolute header path).
if ! command -v mysql_config >/dev/null 2>&1; then
    echo "error: mysql_config not found — install libmysqlclient-dev (Debian/Ubuntu) or mysql-devel (RHEL/Fedora)" >&2
    exit 1
fi

"$SWIFT" build -c release

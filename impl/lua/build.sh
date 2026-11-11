#!/usr/bin/env bash
# Build step for the Lua implementation: nothing to compile, just verify that
# LuaJIT and the libmysqlclient shared library are available.
set -euo pipefail

if ! command -v luajit >/dev/null 2>&1; then
    echo "luajit not found: install LuaJIT (e.g. apt install luajit)" >&2
    exit 1
fi

# Probe the same ffi.load fallback chain bench.lua uses.
if ! luajit -e '
local ffi = require("ffi")
local names = { "mysqlclient", "libmysqlclient.so.24",
                "libmysqlclient.so.21", "libmysqlclient.so" }
for _, name in ipairs(names) do
    if pcall(ffi.load, name) then os.exit(0) end
end
os.exit(1)' >/dev/null 2>&1; then
    echo "libmysqlclient shared library not found: install the MySQL client library (e.g. apt install libmysqlclient-dev or libmysqlclient21)" >&2
    exit 1
fi

exit 0

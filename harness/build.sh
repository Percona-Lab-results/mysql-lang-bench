#!/usr/bin/env bash
# Usage: build.sh <host> [lang]   — build the host's language on the remote host.
source "$(dirname "$0")/lib.sh"
HOST=$1
LANG_IMPL=${2:-$(lang_for_host "$HOST")}
rssh "$HOST" "cd $REMOTE_DIR && bash impl/$LANG_IMPL/build.sh"
echo "build OK: $LANG_IMPL on $HOST"

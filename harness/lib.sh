# Shared helpers, sourced by local harness scripts.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/config/ssh.env"

lang_for_host() {
    local lang
    lang=$(awk -v h="$1" '!/^#/ && $1==h {print $2}' "$ROOT/config/hosts.conf")
    [ -n "$lang" ] || { echo "no language mapped to host $1 in config/hosts.conf" >&2; return 1; }
    echo "$lang"
}

langs_for_host() {
    local langs
    langs=$(awk -v h="$1" '!/^#/ && $1==h {$1=""; print}' "$ROOT/config/hosts.conf")
    [ -n "${langs// /}" ] || { echo "no languages mapped to host $1 in config/hosts.conf" >&2; return 1; }
    echo $langs
}

all_hosts() { awk '!/^#/ && NF {print $1}' "$ROOT/config/hosts.conf"; }

rssh() {
    local host=$1; shift
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$SSH_USER@$host" "$@"
}

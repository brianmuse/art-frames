#!/bin/sh
# lan-access.sh <base> on|off <host>: open Art Frames on the home network, or close it again.
# It changes only the DAYFRAME_LISTEN and DAYFRAME_HOSTS lines in <base>/.env, and it keeps
# the owner and mode of .env. Run it with sudo. Restart the project after it.
set -eu
base=$1
mode=$2
host=$3
env_file="$base/.env"
case "$mode" in on | off) ;; *) echo "Use on or off" >&2; exit 2 ;; esac
if ! test -f "$env_file" || test -L "$env_file"; then
    echo "$env_file is missing or not a plain file" >&2
    exit 1
fi
# shellcheck disable=SC2012 # A plain file name; find has no portable owner print.
owner=$(ls -nd "$env_file" | awk '{ print $3 ":" $4 }')
temp=$(mktemp "$base/.environment.XXXXXX")
trap 'rm -f "$temp"' EXIT
# Keep every other line. On "on", add "*" in front of any host names already listed.
# On "off", take "*" out again and drop the line when nothing is left.
awk -v mode="$mode" '
    /^DAYFRAME_LISTEN=/ { next }
    /^DAYFRAME_HOSTS=/ { hosts = substr($0, 16); found = 1; next }
    { print }
    END {
        if (mode == "on") {
            print "DAYFRAME_LISTEN=0.0.0.0"
            if (!found || hosts == "") hosts = "*"
            else if (hosts != "*" && hosts !~ /^\*,/ && hosts !~ /,\*(,|$)/) hosts = "*," hosts
            print "DAYFRAME_HOSTS=" hosts
        } else if (found) {
            if (hosts == "*") hosts = ""
            sub(/^\*,/, "", hosts)
            if (hosts != "") print "DAYFRAME_HOSTS=" hosts
        }
    }
' "$env_file" > "$temp"
chown "$owner" "$temp"
chmod 600 "$temp"
mv "$temp" "$env_file"
trap - EXIT
port=$(sed -n 's/^DAYFRAME_PORT=\([0-9][0-9]*\)$/\1/p' "$env_file" | tail -n 1)
if test "$mode" = on; then
    echo "Home network access: on. After the restart, open http://$host:${port:-8765}"
else
    echo "Home network access: off. Use npm run open (the SSH tunnel) again."
fi

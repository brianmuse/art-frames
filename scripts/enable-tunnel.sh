#!/bin/sh
# Explicit administrator action: permit one user's local forwards to Art Frames only.
set -eu
umask 077
test "$(id -u)" = 0 || { echo 'Run this operation through sudo' >&2; exit 1; }
account=${1:?SSH account required}
case "$account" in ''|*[!A-Za-z0-9_-]*|[-]*) echo 'Invalid SSH account' >&2; exit 1 ;; esac
test "$(id -u "$account")" -ne 0 || { echo 'Use a nonroot SSH account' >&2; exit 1; }
source_dir=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)
base=$(dirname "$source_dir")
test "$(basename "$source_dir")" = source
test ! -L "$base/.dayframe-managed"
test "$(cat "$base/.dayframe-managed")" = dayframe-managed-source-v1
config=/etc/ssh/sshd_config
test -f "$config" && test ! -L "$config"
# Other match criteria or included files can apply a different policy to LAN clients.
awk '
    { gsub(/=/, " ") }
    $1 !~ /^#/ && $1 ~ /"/ { exit 1 }
    tolower($1) == "include" { exit 1 }
    tolower($1) == "match" {
        if (NF != 3 || tolower($2) != "user" || $3 !~ /^[A-Za-z0-9_-]+$/) exit 1
    }
' "$config" || {
    echo 'Conditional or included SSH policy needs manual review; no changes made' >&2
    exit 1
}
sshd=$(command -v sshd)
context="user=$account,host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=22"
original=$(cksum < "$config")
# sshd -T prints keywords in lower case in older OpenSSH (allowtcpforwarding) and in mixed case in
# newer OpenSSH (AllowTcpForwarding). Lower the keyword only, so the checks below work with both.
lower_keywords() { printf '%s\n' "$1" | awk '{ $1 = tolower($1); print }'; }
current=$("$sshd" -T -f "$config" -C "$context")
current=$(lower_keywords "$current")
pid=$(cat /run/sshd.pid)
case "$pid" in ''|*[!0-9]*) echo 'Invalid SSH daemon PID' >&2; exit 1 ;; esac
test "$(cat "/proc/$pid/comm")" = sshd
"$sshd" -t -f "$config"
if printf '%s\n' "$current" | grep -qx 'allowtcpforwarding local' && \
   printf '%s\n' "$current" | grep -qx 'permitopen 127.0.0.1:8765' && \
   printf '%s\n' "$current" | grep -qx 'disableforwarding no'; then
    kill -HUP "$pid"
    echo 'The app-only forwarding rule is already present. Reconnect SSH to use it.'
    exit 0
fi
printf '%s\n' "$current" | grep -qx 'allowtcpforwarding no' || {
    echo 'Existing forwarding policy needs manual review; no changes made' >&2
    exit 1
}
marker="# Art Frames tunnel for $account"
# Rules written before the rename carry the old marker.
if grep -qF "$marker" "$config" || grep -qF "# Dayframe tunnel for $account" "$config"; then
    echo 'An existing Art Frames rule needs manual review; no changes made' >&2
    exit 1
fi
candidate=$(mktemp /etc/ssh/.dayframe.XXXXXX)
trap 'rm -f "$candidate"' EXIT
trap 'exit 1' HUP INT TERM
cp -p "$config" "$candidate"
cat >> "$candidate" <<EOF

$marker
Match User $account
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:8765
EOF
"$sshd" -t -f "$candidate"
effective=$("$sshd" -T -f "$candidate" -C "$context")
effective=$(lower_keywords "$effective")
if ! printf '%s\n' "$effective" | grep -qx 'allowtcpforwarding local' ||
   ! printf '%s\n' "$effective" | grep -qx 'permitopen 127.0.0.1:8765' ||
   ! printf '%s\n' "$effective" | grep -qx 'disableforwarding no'; then
    echo 'Existing forwarding policy conflicts with the app-only rule; no changes made' >&2
    exit 1
fi
test "$(cksum < "$config")" = "$original" || { echo 'SSH configuration changed during validation; stopping' >&2; exit 1; }
backups="$base/ssh-backups"
test ! -L "$backups"
mkdir -p "$backups"
chmod 700 "$backups"
backup="$backups/sshd_config.$(date +%Y%m%d-%H%M%S).$$"
cp -p "$config" "$backup"
mv "$candidate" "$config"
if ! kill -HUP "$pid"; then
    cp -p "$backup" "$config"
    echo 'SSH reload failed; original configuration restored' >&2
    exit 1
fi
echo 'Installed app-only SSH forwarding to 127.0.0.1:8765.'
echo "Original SSH configuration saved in: $backup"
echo 'Reconnect the SSH control connection before opening the tunnel.'

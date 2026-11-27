#!/bin/sh
# The Art Frames rename: check the NAS install, print what stays and what changes, and make
# one backup. The NAS keeps every name (folder, Docker project, images, markers, .env keys),
# so this script moves, renames and deletes nothing.
#
# Usage, as root from the source folder: rename-check.sh [--dry-run] [--auto] <compose profile>
#   --dry-run  checks and the plan only; no backup.
#   --auto     update-remote.sh runs it like this: silent once the backup is done.
# Exit 1: a check said STOP. Nothing changed.
set -eu
umask 077
test "$(id -u)" = 0 || { echo 'Run this check through sudo' >&2; exit 1; }
dry_run=""
auto=""
compose=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        --auto) auto=1 ;;
        -*) echo "Unknown option $arg" >&2; exit 1 ;;
        *) compose=$arg ;;
    esac
done
source_dir=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd -P)
base=$(dirname "$source_dir")
PATH="$PATH:/usr/local/bin"
export PATH
cd "$source_dir"
. scripts/compose-files.sh
. scripts/previous-state.sh
mode=$(compose_mode "$compose")
renderer=dayframe-renderer
test "$mode" = browser || renderer=dayframe-renderer-static
done_file="$base/.rename-backup-done"

if test -n "$auto" && test -f "$done_file"; then exit 0; fi
if test -n "$auto" && ! test -e "$data/settings.json"; then
    # A new install has nothing from before the rename to back up.
    date -u > "$done_file"
    exit 0
fi

take_lock
trap release_lock EXIT
trap 'exit 1' HUP INT TERM

stops=0
ok() { echo "OK    $1"; }
warn() { echo "WARN  $1"; }
stop() { echo "STOP  $1"; stops=$((stops + 1)); }
real_dir() { test -d "$1" && ! test -L "$1"; }

echo 'Checks (read only):'
case "$(basename "$base")" in
    dayframe|art-frames) ;;
    *) stop "The install folder $base must end in /dayframe" ;;
esac
if real_dir "$base"; then ok "Install folder: $base"; else stop "$base is not a real folder"; fi
if ! test -L "$base/.dayframe-managed" && test "$(cat "$base/.dayframe-managed" 2>/dev/null)" = dayframe-managed-source-v1; then
    ok 'Marker .dayframe-managed holds dayframe-managed-source-v1'
else
    stop 'Marker .dayframe-managed is missing or wrong'
fi
for name in data sites .secrets; do
    if real_dir "$base/$name"; then ok "$name/ is a folder"; else stop "$base/$name is missing or a link"; fi
done
if plain_file "$env_file"; then ok '.env is a file'; else stop '.env is missing or not a plain file'; fi
if test -f "$base/config.local.toml" && ! test -L "$base/config.local.toml"; then
    ok 'config.local.toml is a file'
else
    warn 'config.local.toml is missing or a link'
fi
if grep -qx 'DAYFRAME_UID=10001' "$env_file" 2>/dev/null && grep -qx 'DAYFRAME_GID=10001' "$env_file" 2>/dev/null; then
    ok '.env has DAYFRAME_UID=10001 and DAYFRAME_GID=10001'
else
    stop '.env needs the lines DAYFRAME_UID=10001 and DAYFRAME_GID=10001'
fi
# Count the TV tokens only. Never print their names or contents.
if real_dir "$data/tokens"; then
    ok "TV tokens: $(find "$data/tokens" -maxdepth 1 -type f -name '*.txt' | wc -l | tr -d ' ') in data/tokens"
else
    warn 'No data/tokens folder: no TV is paired'
fi
if ids=$(controller_id); then
    count=$(printf '%s\n' "$ids" | grep -c . || true)
    if test "$count" = 1; then ok 'One controller is running (project dayframe, service dayframe)'; else warn 'No controller is running'; fi
else
    stop 'More than one controller is running'
fi
# Read the Compose labels, so the answer does not depend on the folder or on .env.
services=$(docker ps --filter label=com.docker.compose.project=dayframe --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null | sort -u | tr '\n' ' ' || true)
ok "Compose project dayframe runs: ${services:-nothing}"
for image in dayframe "$renderer" dayframe-website; do
    if docker image inspect "$image:local" >/dev/null 2>&1; then ok "Image $image:local exists"; else warn "No image $image:local"; fi
done
if docker image inspect dayframe:previous >/dev/null 2>&1; then
    ok 'Image dayframe:previous exists: npm run rollback has a version to go back to'
else
    warn 'No dayframe:previous image yet'
fi
if test -f "$saved/unfinished"; then stop 'An update did not finish (.previous/unfinished). Run the update again first'; fi
if unfinished_swap "$saved/sites"; then stop 'A swap of layout folders did not finish. Run the update again first'; fi
if test -n "$(docker ps -q --filter label=com.docker.compose.project=art-frames 2>/dev/null || true)"; then
    stop 'Containers run under a Compose project named art-frames. Only the project dayframe may run'
fi
parts=""
for name in data sites .previous; do
    if test -e "$base/$name"; then parts="$parts $base/$name"; fi
done
# shellcheck disable=SC2086
used=$(du -sk $parts 2>/dev/null | awk '{ total += $1 } END { print total + 0 }')
free=$(df -Pk "$base" | awk 'NR == 2 { print $4 }')
if test "$free" -ge $((used * 2)); then
    ok "Free space: ${free} KB, backup needs about ${used} KB"
else
    stop "Not enough free space: ${free} KB free, need $((used * 2)) KB"
    echo "      To skip the backup, run: sudo sh -c 'date -u > $done_file'"
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$base/.rename-backup-$stamp"
echo
echo 'Plan:'
echo '  STAYS:   the NAS folder, the Docker project "dayframe", its service, images and volumes,'
echo '           the marker files, the .env keys, data/, sites/, the TV tokens and .previous'
echo '  CHANGES: the name in the UI, and the code package inside the new images (art_frames)'
if test -n "$dry_run"; then
    echo '  BACKUP:  skipped: --dry-run'
elif test -f "$done_file"; then
    echo "  BACKUP:  skipped: already done on $(cat "$done_file")"
else
    echo "  BACKUP:  $backup/"
fi

if test "$stops" -gt 0; then
    echo "$stops check(s) said STOP. Nothing changed." >&2
    exit 1
fi
if test -n "$dry_run" || test -f "$done_file"; then exit 0; fi

# A copy of the running system: settings.json is written atomically, and update-remote.sh
# still saves a stopped-controller copy of it in .previous. The backup holds secrets and is
# for a disaster restore only; npm run rollback uses .previous.
make_backup() {
    set -e
    mkdir -m 700 "$backup"
    cp -p "$env_file" "$backup/env"
    if test -f "$base/config.local.toml"; then cp -p "$base/config.local.toml" "$backup/config.local.toml"; fi
    if test -f "$sites/compose.apps.yaml"; then cp -p "$sites/compose.apps.yaml" "$backup/compose.apps.yaml"; fi
    names=""
    for name in data sites .secrets .previous .dayframe-managed; do
        if test -e "$base/$name"; then names="$names $name"; fi
    done
    # SQLite -wal and -shm files change while the controller runs; skip them.
    # GNU tar exits 1 when a file changed while it read it (data/ is live): only a warning.
    # Other tars use exit 1 for a real error.
    if tar --version 2>/dev/null | grep -q 'GNU tar'; then
        # shellcheck disable=SC2086
        tar -C "$base" --warning=no-file-changed --exclude='*-wal' --exclude='*-shm' \
            -cpf "$backup/runtime.tar" $names || test $? -eq 1
    else
        # shellcheck disable=SC2086
        tar -C "$base" --exclude='*-wal' --exclude='*-shm' -cpf "$backup/runtime.tar" $names
    fi
    docker ps --filter label=com.docker.compose.project=dayframe --format json > "$backup/compose-ps.json" 2>/dev/null || true
    docker image ls --filter 'reference=dayframe*' > "$backup/images.txt" 2>/dev/null || true
    (cd "$backup" && sha256sum env runtime.tar ./*.json ./*.txt ./*.toml ./*.yaml 2>/dev/null > SHA256SUMS || true)
}

# A failed backup leaves no partial folder with secrets in it.
trap 'status=$?; test -f "$done_file" || rm -rf "$backup"; release_lock; exit "$status"' EXIT
set +e
(make_backup)
status=$?
set -e
if test "$status" -ne 0; then
    rm -rf "$backup"
    if test -n "$auto"; then
        # The backup is a safety net, not a gate: the update goes on, and the next one tries again.
        echo "WARN  The rename backup failed (exit $status). The update goes on without it." >&2
        exit 0
    fi
    echo "The backup failed (exit $status). Nothing changed." >&2
    exit 1
fi
date -u > "$done_file"
echo "Backup saved: $backup ($(du -sk "$backup" | awk '{ print $1 }') KB)"

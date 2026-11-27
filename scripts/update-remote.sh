#!/bin/sh
# One administrator session for the managed installation's complete update.
# Exit 3: the update failed after activation, and the previous version is back and healthy.
set -eu
test "$(id -u)" = 0 || { echo 'Run this update through sudo' >&2; exit 1; }
compose=${1:-}
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

phase=start
tagged=""
new_apps=""
controller=""
# Until activation, a failure puts back the saved files, layout folders and images.
# The running containers never changed.
finish() {
    status=$?
    trap - EXIT
    trap '' HUP INT TERM
    after_swap=''
    if test "$status" != 0 && { test "$phase" = before || test "$phase" = stopped; }; then
        restored=1
        restore_files || {
            restored=""
            echo 'Could not put back .env and the layout override. Check them before the next update.' >&2
        }
        restore_sites "$saved/sites" || {
            restored=""
            sites_not_restored "$saved/sites" 'Run the update or the rollback again; it puts them back first.'
        }
        for name in $tagged; do docker tag "$name:previous" "$name:local" || true; done
        for id in $new_apps; do docker image rm "dayframe-app-$id:local" >/dev/null 2>&1 || true; done
        if test -n "$restored"; then rm -f "$saved/unfinished" || true; fi
        started=1
        if test "$phase" = stopped && test -n "$controller"; then
            docker start "$controller" >/dev/null || started=""
        fi
        if test -z "$started"; then
            echo "The previous version could not be started again. Run: sudo docker start $controller" >&2
        elif test -z "$tagged"; then
            echo 'Nothing was started.' >&2
        elif test -n "$restored"; then
            echo 'Nothing changed on the TVs. The previous version is still running.' >&2
        else
            echo 'The previous version is still running, but its files were not all put back.' >&2
        fi
    fi
    release_lock
    exit "$status"
}
take_lock
trap finish EXIT
trap 'exit 1' HUP INT TERM
recover_update 'Run the update again.'
# The Art Frames rename: one backup before the first update after it. Silent after that.
DAYFRAME_LOCK_HELD=1 sh scripts/rename-check.sh --auto "$compose"

# The layout set that runs now. remote.py transferred the new layouts to sites/.incoming, and
# .env and the override still describe the running containers.
staged=$(check_incoming)
if test -n "$staged" && test "$mode" = browser; then
    rm -rf "$incoming"
    staged=""
    echo 'Removed the layouts in sites/.incoming: a browser Compose profile runs no layout.' >&2
fi
old_files=$(compose_files "$compose")
old_extra=$(extra_app_ids "$compose")
old_first=$(first_app_id)
controller=$(controller_id)
# Keep the running images as :previous so a failed start can roll back.
# Only images that were Art Frames' own :previous are cleaned up later.
previous="dayframe:previous $renderer:previous dayframe-website:previous"
pairs="dayframe:dayframe renderer:$renderer website:dayframe-website"
for id in $old_extra; do
    previous="$previous dayframe-app-$id:previous"
    pairs="$pairs app-$id:dayframe-app-$id"
done
# $old_files, $previous and $pairs hold only validated names, so they split safely.
# shellcheck disable=SC2086
stale=$(docker image inspect --format '{{.Id}}' $previous 2>/dev/null || true)
for pair in $pairs; do
    service=${pair%%:*}
    name=${pair#*:}
    # shellcheck disable=SC2086
    container=$(docker compose -p dayframe $old_files ps -q "$service" 2>/dev/null | head -n 1)
    test -n "$container" || continue
    image=$(docker inspect --format '{{.Image}}' "$container") || continue
    docker tag "$image" "$name:previous"
    tagged="$tagged $name"
done
phase=before
# One line of IDs; $old_extra holds only validated IDs.
# shellcheck disable=SC2086,SC2116
save_state "$old_first" "$(echo $old_extra)"
# Until the new version starts, a later run that finds this file puts back the saved
# files and layout folders: this run stopped before it could do that itself.
: > "$saved/unfinished"
# Prepare and build from the transferred layouts in sites/.incoming. The running controller
# reads sites/, so the layout folders there change only just before it stops (see below).
# prepare-remote.sh writes .env and the override with the final sites/ paths; these
# variables point the reads and the builds at the transferred folders.
if test -n "$staged"; then
    DAYFRAME_FIRST_DIR="$incoming/installed"
    DAYFRAME_SITE_BUILD_CONTEXT=./sites/.incoming/installed
    export DAYFRAME_FIRST_DIR DAYFRAME_SITE_BUILD_CONTEXT
    # shellcheck disable=SC2086
    if in_list apps $staged; then
        DAYFRAME_APPS_DIR="$incoming/apps"
        DAYFRAME_APPS_CONTEXT=./sites/.incoming/apps
        export DAYFRAME_APPS_DIR DAYFRAME_APPS_CONTEXT
    fi
fi
sh scripts/prepare-remote.sh "$mode"
files=$(compose_files "$compose")
extra=$(extra_app_ids "$compose")
for id in $extra; do
    # shellcheck disable=SC2086
    in_list "$id" $old_extra || new_apps="$new_apps $id"
done
sh scripts/build-check.sh "$compose"
unset DAYFRAME_FIRST_DIR DAYFRAME_SITE_BUILD_CONTEXT DAYFRAME_APPS_DIR DAYFRAME_APPS_CONTEXT
# Save the controller settings with the :previous images. The new version can save
# settings that the previous version rejects, so a rollback restores this copy.
# Per-TV settings live in the same settings.json, in its displays list.
# Stop the controller first, so nothing writes the file during the copy. The container
# ID needs no Compose file, so this works when the override is gone.
# Just before that, put the transferred layouts in place. The running ones move to
# .previous/sites, where a failure or a rollback finds them.
if test -n "$staged"; then place_incoming "$saved/sites"; fi
phase=stopped
if test -n "$controller"; then docker stop "$controller" >/dev/null; fi
save_settings
# From here a lost SSH connection must not stop the run halfway: it finishes, and rolls
# back when the new version is not healthy. Ignore it before the marker goes, so a
# hang-up can't leave the controller stopped with no marker for the next run.
trap '' HUP
phase=after
rm -f "$saved/unfinished"
# --remove-orphans stops the container of a layout that was removed from the list.
# shellcheck disable=SC2086
if docker compose -p dayframe $files up -d --remove-orphans && sh scripts/wait-healthy.sh "$compose"; then
    for id in $stale; do
        test -n "$(docker image inspect --format '{{join .RepoTags ""}}' "$id" 2>/dev/null)" || docker image rm "$id" >/dev/null 2>&1 || true
    done
    # Tags that nothing uses any more: :local of a layout that is gone, and :previous of a
    # layout that did not run before this update.
    for ref in $(docker image ls --format '{{.Repository}}:{{.Tag}}' --filter 'reference=dayframe-app-*' 2>/dev/null || true); do
        id=${ref#dayframe-app-}
        id=${id%:*}
        # shellcheck disable=SC2086
        case "$ref" in
            *:local) in_list "$id" $extra || docker image rm "$ref" >/dev/null 2>&1 || true ;;
            *:previous) in_list "$id" $old_extra || docker image rm "$ref" >/dev/null 2>&1 || true ;;
        esac
    done
    exit 0
fi
if docker image inspect dayframe:previous >/dev/null 2>&1; then
    echo 'The new version failed its health check. Rolling back to the previous images…' >&2
    if DAYFRAME_LOCK_HELD=1 sh scripts/rollback-remote.sh --yes "$compose"; then
        exit 3
    fi
    echo 'Rollback also failed.' >&2
fi
exit 1

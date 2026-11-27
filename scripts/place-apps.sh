#!/bin/sh
# For a separate layout install (install-layout.py --remote): put the transferred layouts in
# place, then prepare the runtime. remote.py update uses update-remote.sh instead, which
# keeps the replaced layouts for a rollback.
# Usage: place-apps.sh static|browser
#
# prepare-remote.sh reads the transferred layouts in sites/.incoming first. Only when that
# works do the layouts move into place. The replaced folders move to .replaced-sites first and
# are removed only after the new folders are in place. If this script stops halfway, the
# next run puts them back.
set -eu
test "$(id -u)" = 0 || { echo 'Run this through sudo' >&2; exit 1; }
mode=${1:-}
source_dir=$(CDPATH='' cd -- "$(dirname "$0")/.." && pwd -P)
base=$(dirname "$source_dir")
cd "$source_dir"
. scripts/previous-state.sh
replaced="$base/.replaced-sites"
take_lock
trap release_lock EXIT
trap 'exit 1' HUP INT TERM
test ! -L "$replaced"
# A stopped update or an earlier run of this script may have left a swap halfway.
recovered=""
recover_update 'Run place-apps.sh again.' || exit 1
recover_sites "$replaced" 'Run place-apps.sh again.' || exit 1
rm -rf "$replaced"
if test -n "$recovered"; then
    # Make .env and the override match the layout folders that are back in place.
    sh scripts/prepare-remote.sh "$mode"
fi
staged=$(check_incoming)
if test -n "$staged" && test "$mode" = browser; then
    rm -rf "$incoming"
    staged=""
    echo 'Removed the layouts in sites/.incoming: a browser Compose profile runs no layout.' >&2
fi
if test -z "$staged"; then
    sh scripts/prepare-remote.sh "$mode"
    exit 0
fi
# .env and the override name the final folders, so they are right after the move.
apps_dir="$sites/apps"
# shellcheck disable=SC2086
if in_list apps $staged; then apps_dir="$incoming/apps"; fi
DAYFRAME_FIRST_DIR="$incoming/installed" DAYFRAME_APPS_DIR="$apps_dir" sh scripts/prepare-remote.sh "$mode"
if ! place_incoming "$replaced"; then
    restore_sites "$replaced" || {
        sites_not_restored "$replaced" 'Run place-apps.sh again.'
        exit 1
    }
    # Write .env and the override for the installed layouts again.
    sh scripts/prepare-remote.sh "$mode" || true
    echo 'Could not move the new layouts into place. The installed layouts stay as they are.' >&2
    exit 1
fi
rm -rf "$replaced"

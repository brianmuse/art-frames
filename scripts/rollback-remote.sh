#!/bin/sh
# Restart the images that ran before the last update, with the .env, the layout override,
# the layout folders and the settings saved before it. Layout settings stay as they are.
# Usage: rollback-remote.sh [--yes] <compose profile>
# --yes restores settings older than one hour without asking. update-remote.sh uses it.
set -eu
test "$(id -u)" = 0 || { echo 'Run this rollback through sudo' >&2; exit 1; }
confirmed=""
if test "${1:-}" = --yes; then
    confirmed=1
    shift
fi
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
take_lock
trap release_lock EXIT
trap 'exit 1' HUP INT TERM
# A swap that stopped halfway never activated, so put the old folders back first.
recover_update 'Run the rollback again.' || exit 1
docker image inspect dayframe:previous >/dev/null 2>&1 || { echo 'No previous Art Frames images to roll back to.' >&2; exit 1; }
# The previous images need the previous layout folders. The update kept them in
# .previous/sites. Without that copy, the layouts in sites/ must still be the old ones.
if test -f "$saved/app-set" && ! test -d "$saved/sites"; then
    old_first=$(sed -n 's/^first //p' "$saved/app-set")
    old_extra=$(sed -n 's/^extra //p' "$saved/app-set")
    missing=""
    if test -n "$old_first" && test "$(installed_id "$sites/installed")" != "$old_first"; then
        missing="$old_first"
    fi
    for id in $old_extra; do
        test "$(installed_id "$sites/apps/$id")" = "$id" || missing="$missing $id"
    done
    if test -n "$missing"; then
        echo "These layouts from before the update are not in sites/: $missing. Rollback can't bring them back." >&2
        echo 'To go back, redeploy the previous commit of Art Frames and of your layouts.' >&2
        exit 1
    fi
fi
if test -f "$saved/settings.json" && ! settings_replaceable; then
    echo 'data/settings.json is not a plain file. Refusing to replace it.' >&2
    exit 1
fi
# Ask before settings that are more than one hour old replace the current ones.
if test -f "$saved/settings.json"; then
    saved_at=$(cat "$saved/saved-at" 2>/dev/null || echo 0)
    case "$saved_at" in ''|*[!0-9]*) saved_at=0 ;; esac
    minutes=$(( ($(date +%s) - saved_at) / 60 ))
    echo "The saved settings are $minutes minutes old. Restoring them removes the settings changes since then."
    if test "$minutes" -ge 60 && test -z "$confirmed"; then
        printf 'Type yes to restore them: '
        read -r answer || answer=""
        test "$answer" = yes || { echo 'Rollback stopped. Nothing changed.'; exit 1; }
    fi
fi
# Older builds can't read settings saved with the new layout schedule ("week"). An image
# that can read them has the label art-frames.settings=week (see the Dockerfile).
# Without a saved copy, and only when the previous image lacks that label, the rollback
# moves the current file aside to data/settings.week-<time>.json. Then it puts back
# data/settings.pre-week.json, the copy the controller kept before its first save in
# that shape, when there is one.
week_settings=""
week_shape=""
if ! test -f "$saved/settings.json" && test -f "$data/settings.json" && grep -q '"week":' "$data/settings.json"; then
    # A failed inspect is not a missing label: stop rather than guess.
    if ! previous_label=$(docker image inspect --format '{{index .Config.Labels "art-frames.settings"}}' dayframe:previous); then
        echo "Could not read the label of the previous Art Frames image. Nothing changed. Check that Docker runs, then run the rollback again." >&2
        exit 1
    fi
    test "$previous_label" = week || week_shape=1
fi
if test -n "$week_shape"; then
    settings_replaceable || { echo 'data/settings.json is not a plain file. Refusing to replace it.' >&2; exit 1; }
    week_settings=1
    if plain_file "$data/settings.pre-week.json"; then
        # Ask before an old copy replaces the current settings, the same as above.
        kept_at=$(date -r "$data/settings.pre-week.json" +%s 2>/dev/null || stat -c %Y "$data/settings.pre-week.json" 2>/dev/null || echo 0)
        case "$kept_at" in ''|*[!0-9]*) kept_at=0 ;; esac
        minutes=$(( ($(date +%s) - kept_at) / 60 ))
        echo "The previous version can't read the current settings. The settings from before the new layout schedule are $minutes minutes old."
        if test "$minutes" -ge 60 && test -z "$confirmed"; then
            printf 'Type yes to restore them: '
            read -r answer || answer=""
            test "$answer" = yes || { echo 'Rollback stopped. Nothing changed.'; exit 1; }
        fi
    fi
fi
# Find the controller while its image still has the dayframe:local tag.
controller=$(controller_id)
restore_files
restore_sites "$saved/sites" || {
    sites_not_restored "$saved/sites" 'Run the rollback again.'
    exit 1
}
files=$(compose_files "$compose")
extra=$(extra_app_ids "$compose")
images="dayframe $renderer dayframe-website"
for id in $extra; do
    images="$images dayframe-app-$id"
done
local_images=""
for image in $images; do
    local_images="$local_images $image:local"
done
# The images being replaced; removed after a healthy rollback if nothing else tags them.
# $files, $images and $local_images hold only validated names, so they split safely.
# shellcheck disable=SC2086
replaced=$(docker image inspect --format '{{.Id}}' $local_images 2>/dev/null || true)
for image in $images; do
    if docker image inspect "$image:previous" >/dev/null 2>&1; then
        docker tag "$image:previous" "$image:local"
    fi
done
restored=""
if test -f "$saved/settings.json"; then
    # Stop the controller first, so it cannot save over the restored file.
    if test -n "$controller"; then docker stop "$controller" >/dev/null; fi
    if ! restore_settings; then
        if test -n "$controller"; then docker start "$controller" >/dev/null || true; fi
        echo 'Could not restore the settings. Art Frames was started again with the current settings.' >&2
        exit 1
    fi
    restored=1
elif test -n "$week_settings"; then
    if test -n "$controller"; then docker stop "$controller" >/dev/null; fi
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    moved_to="data/settings.week-$stamp.json"
    # undo_week_move: put the current settings back over data/settings.json, start the
    # controller again and say what happened.
    undo_week_move() {
        if test -f "$data/settings.week-$stamp.json"; then
            if mv "$data/settings.week-$stamp.json" "$data/settings.json"; then
                echo 'Could not replace the settings. They are back as they were.' >&2
            else
                echo "Could not replace the settings. The current settings are in $moved_to. Move them back to data/settings.json." >&2
            fi
        else
            echo 'Could not replace the settings. They did not change.' >&2
        fi
        if test -n "$controller"; then docker start "$controller" >/dev/null || true; fi
        echo 'Art Frames was started again with the settings above.' >&2
    }
    # A signal during the move puts the current settings back too.
    trap 'trap - HUP INT TERM; undo_week_move; exit 1' HUP INT TERM
    # Keep the current settings first. Then put back the copy from before the week, and
    # rename that copy, so the next update's first save keeps a new one.
    mv "$data/settings.json" "$data/settings.week-$stamp.json" && {
        if plain_file "$data/settings.pre-week.json"; then
            copy_into "$data/settings.pre-week.json" "$data/settings.json" \
                && mv "$data/settings.pre-week.json" "$data/settings.pre-week.restored-$stamp.json" \
                && week_settings=restored
        else
            week_settings=moved
        fi
    } || {
        trap 'exit 1' HUP INT TERM
        undo_week_move
        exit 1
    }
    trap 'exit 1' HUP INT TERM
fi
# --remove-orphans stops the container of a layout that is not in the restored layout list,
# the same as update-remote.sh.
# shellcheck disable=SC2086
docker compose -p dayframe $files up -d --remove-orphans
sh scripts/wait-healthy.sh "$compose"
for id in $replaced; do
    test -n "$(docker image inspect --format '{{join .RepoTags ""}}' "$id" 2>/dev/null)" || docker image rm "$id" >/dev/null 2>&1 || true
done
echo 'Rolled back to the previous images.'
if test -n "$restored"; then
    echo 'Settings returned to their state before the update. Changes saved after the update are gone.'
elif test "$week_settings" = restored; then
    echo 'Put back the settings from before the new layout schedule. Changes saved since then are not used.'
    echo "The current settings moved to $moved_to."
elif test -n "$week_settings"; then
    echo "The previous version can't read the current settings. They moved to $moved_to."
    echo 'Art Frames starts from the installation defaults. Set it up again on the settings page.'
else
    echo 'No saved settings from before the update, so settings stay as they are.'
fi

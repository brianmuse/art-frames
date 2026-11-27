#!/bin/sh
# Keep a layout's saved settings when its source was renamed.
# Usage: sh scripts/keep-layout-settings.sh <data folder> <layout folder> [<layout folder> ...]
#
# A layout's settings scope is "<id>-<32 hex>", and the hex part comes from the layout's
# source. When a source is renamed (for example, a renamed GitHub repository), the layout
# gets a new, empty scope. install-layout.py writes the scopes that the layout had under the
# earlier names of its source to <layout folder>/.earlier-scopes, one per line. Only those
# scopes are used: a different source that reuses a layout ID never gets its settings.
#
# For each layout folder, when data/layout-settings/<scope> has no config.json, copy the
# earlier scope with the newest config.json into <scope>. Also copy data/layout-state of that
# scope when the new state folder is missing or empty. It only copies: the earlier folders
# stay, and nothing in <scope> is replaced. It copies only regular files and folders. A folder
# with a link or any other file type is not used. A second run does nothing, because <scope>
# then has a config.json. One layout's failure prints a warning; the other layouts still run.
#
# A running layout container can write to its own state folder. So the copy goes first to a
# private temporary folder that no container mounts. Then each entry moves into place with a
# rename, which replaces a name but never writes through a link. The new folder keeps its
# inode, so a running container sees the files without a restart.
# Run it as root on the NAS (prepare-remote.sh) or as the owner locally (install-layout.py).
# cp -p keeps each file's owner (the container user) and mode.
. "$(dirname "$0")/app-profile.sh"
set -eu
umask 077

if test "${1:-}" != --one; then
    data=${1:?Usage: keep-layout-settings.sh <data folder> <layout folder> ...}
    shift
    for folder in "$@"; do
        # A separate process for each layout, so set -e stops only that layout.
        if ! sh "$0" --one "$data" "$folder"; then
            app_id=$(basename "$folder")
            read_profile "$folder/.install-profile" || true
            echo "Layout $app_id: could not restore its saved settings from an earlier install." >&2
        fi
    done
    exit 0
fi
data=$2
folder=$3
settings="$data/layout-settings"
state="$data/layout-state"
temp=""
trap 'test -z "$temp" || rm -rf "$temp"' EXIT
trap 'exit 1' HUP INT TERM

# usable <folder>: true when the folder is a real folder that holds only regular files and
# folders, each file with one link. Then a copy can't follow a link out of the data folder.
# It fails closed: an error from find means the folder is not used.
usable() {
    test -d "$1" && test ! -L "$1" || return 1
    odd=$(find "$1" \( ! -type f ! -type d \) -o \( -type f -links +1 \)) || return 1
    test -z "$odd"
}

# is_empty <folder>: true when the folder is missing or holds nothing.
is_empty() {
    test ! -e "$1" && test ! -L "$1" && return 0
    test -d "$1" && test ! -L "$1" && test -z "$(ls -A "$1")"
}

# move_in <from> <to>: copy the folder <from> into <to> through a temporary folder next to
# <to>. Entries that <to> already has stay as they are. config.json moves last, so a run that
# stops before it is finished by the next run.
move_in() {
    temp=$(mktemp -d "$(dirname "$2")/.keep.XXXXXX")
    cp -RPp "$1/." "$temp/"
    chmod 700 "$temp"
    usable "$temp" || return 1
    if ! test -e "$2" && ! test -L "$2"; then
        mv "$temp" "$2"
        temp=""
        return 0
    fi
    test -d "$2" && test ! -L "$2" || return 1
    for entry in "$temp"/* "$temp"/.[!.]* "$temp"/..?*; do
        test -e "$entry" || continue
        name=$(basename "$entry")
        test "$name" != config.json || continue
        # "mv <entry> <folder>/" renames to <folder>/<name>. It never follows a link there.
        test -e "$2/$name" || test -L "$2/$name" || mv "$entry" "$2/"
    done
    if test -f "$temp/config.json" && ! test -e "$2/config.json" && ! test -L "$2/config.json"; then
        mv "$temp/config.json" "$2/"
    fi
    rm -rf "$temp"
    temp=""
}

test -d "$data" && test ! -L "$data" || exit 0
test -d "$settings" && test ! -L "$settings" || exit 0
test ! -L "$state" || exit 0
test -d "$folder" && test ! -L "$folder" || exit 0
read_profile "$folder/.install-profile" || exit 0
earlier="$folder/.earlier-scopes"
test -f "$earlier" && test ! -L "$earlier" || exit 0
# The scopes are "<first 40 characters of the ID>-<32 hex>", as install-layout.py makes them.
pattern="^$(printf '%.40s' "$app_id")-[0-9a-f]{32}\$"
printf '%s' "$app_scope" | grep -Eq "$pattern" || exit 0
test ! -L "$settings/$app_scope" || exit 0
test ! -L "$state/$app_scope" || exit 0
# The layout already has saved settings.
if test -e "$settings/$app_scope/config.json" || test -L "$settings/$app_scope/config.json"; then
    exit 0
fi
candidates=""
while IFS= read -r name || test -n "$name"; do
    printf '%s' "$name" | grep -Eq "$pattern" || continue
    test "$name" != "$app_scope" || continue
    test -f "$settings/$name/config.json" && test ! -L "$settings/$name/config.json" || continue
    usable "$settings/$name" || continue
    candidates="$candidates $settings/$name/config.json"
done < "$earlier"
test -n "$candidates" || exit 0
# The names are checked above, so they split safely. ls -t lists the newest first.
# shellcheck disable=SC2012,SC2086
newest=$(ls -t $candidates | head -n 1)
old=$(basename "$(dirname "$newest")")
move_in "$settings/$old" "$settings/$app_scope"
if test -d "$state" && usable "$state/$old" && is_empty "$state/$app_scope"; then
    move_in "$state/$old" "$state/$app_scope"
fi
echo "Layout $app_id: restored its saved settings from an earlier install."

# What update-remote.sh saves so that a failed update or a rollback can go back.
# Source this file from the source folder after setting source_dir and base.
#
# $base/.previous is root's own folder, outside data/, which the container user owns.
# It holds the .env and sites/compose.apps.yaml from before the update, the layout set
# that ran then (app-set: a "first <id>" line and an "extra <id> <id> ..." line), the
# layout folders that ran then (sites/installed and sites/apps), and settings.json.
#
# Every step in these functions ends in `|| return 1`: callers run some of them in an
# `if` or `||` list, where `set -e` does not apply.
saved="$base/.previous"
data="$base/data"
sites="$base/sites"
incoming="$sites/.incoming"
lock="$base/.update-lock"
# The signal action after a swap of layout folders. finish() in update-remote.sh sets it to
# '' (ignore), because it is already past the point where a signal may stop the run.
after_swap='exit 1'
env_file="$source_dir/.env"
if test -L "$env_file"; then env_file="$base/.env"; fi

# take_lock: stop when another update or rollback is running. update-remote.sh sets
# DAYFRAME_LOCK_HELD for the rollback it runs itself.
take_lock() {
    if test -n "${DAYFRAME_LOCK_HELD:-}"; then return 0; fi
    test ! -L "$lock" || return 1
    mkdir -m 700 "$lock" 2>/dev/null || {
        echo "Another update is running. If none is, remove $lock and try again." >&2
        return 1
    }
    lock_taken=1
}

release_lock() {
    if test -n "${lock_taken:-}"; then rmdir "$lock" 2>/dev/null || true; fi
}

# in_list <word> <list...>: true when the word is in the list.
in_list() {
    word=$1
    shift
    case " $* " in *" $word "*) return 0 ;; esac
    return 1
}

# plain_file <path>: true for a regular file that is not a link and has no other hard link.
plain_file() {
    test -f "$1" && ! test -L "$1" && test "$(ls -ln "$1" | awk '{ print $2 }')" = 1
}

# copy_into <from> <to>: replace <to> with a copy of <from> through a temp file next to it.
# On any failure the temp file goes and <to> stays as it was.
copy_into() {
    temp=$(mktemp "$(dirname "$2")/.restore.XXXXXX") || return 1
    if cp -p "$1" "$temp" && mv "$temp" "$2"; then return 0; fi
    rm -f "$temp"
    return 1
}

# controller_id: the running controller container, found without Compose files. It must
# run an Art Frames image (dayframe:local or dayframe:previous). Fails when more than one does.
controller_id() {
    ids=$(docker ps -q --filter label=com.docker.compose.project=dayframe \
        --filter label=com.docker.compose.service=dayframe \
        --filter ancestor=dayframe:local --filter ancestor=dayframe:previous 2>/dev/null || true)
    if test "$(printf '%s\n' "$ids" | grep -c .)" -gt 1; then
        echo 'More than one Art Frames controller is running. Stop the extra ones, then try again.' >&2
        return 1
    fi
    printf '%s\n' "$ids" | grep . || true
}

# first_app_id: the first layout's ID from .env, or nothing.
first_app_id() {
    test -f "$env_file" || return 0
    sed -n 's/^DAYFRAME_LAYOUT_ID=//p' "$env_file" | tail -n 1 | grep -E '^[a-z][a-z0-9-]{0,63}$' || true
}

# installed_id <folder>: the layout ID in that folder's .install-profile, or nothing.
installed_id() {
    if test -f "$1/.install-profile" && ! test -L "$1/.install-profile"; then
        cut -d ' ' -f 1 "$1/.install-profile"
    fi
}

# save_state <first layout> <extra layouts>: replace the saved state with the current .env,
# override and layout set.
save_state() {
    test ! -L "$saved" || return 1
    # The only copy of the old layout folders may be in an unfinished swap. Never delete it.
    unfinished_swap "$saved/sites" && {
        echo "$saved/sites holds a swap that did not finish. Refusing to replace it." >&2
        return 1
    }
    test ! -f "$saved/unfinished" || {
        echo "$saved holds an update that did not finish. Refusing to replace it." >&2
        return 1
    }
    rm -rf "$saved" || return 1
    mkdir -m 700 "$saved" || return 1
    if test -f "$env_file"; then cp -p "$env_file" "$saved/env" || return 1; fi
    test ! -L "$sites/compose.apps.yaml" || return 1
    if test -f "$sites/compose.apps.yaml"; then
        cp -p "$sites/compose.apps.yaml" "$saved/compose.apps.yaml" || return 1
    fi
    printf 'first %s\nextra %s\n' "$1" "$2" > "$saved/app-set" || return 1
}

# restore_files: put the saved .env and override back.
restore_files() {
    if test -f "$saved/env"; then copy_into "$saved/env" "$env_file" || return 1; fi
    test ! -L "$sites/compose.apps.yaml" || return 1
    if test -f "$saved/compose.apps.yaml"; then
        copy_into "$saved/compose.apps.yaml" "$sites/compose.apps.yaml" || return 1
    elif test -f "$saved/env"; then
        rm -f "$sites/compose.apps.yaml" || return 1
    fi
}

# The layout folders that install-layout.py transfers to sites/.incoming. Its last step
# writes .incoming/.complete with the names it sent, one per line: installed, and layouts
# when it sent extra layouts. Without that file the transfer did not finish.

# incoming_names: print the names in .incoming/.complete. Fails when the transfer is not
# complete or not valid. Checks every folder before anything moves.
incoming_names() {
    test ! -L "$incoming" && test -d "$incoming" || return 1
    plain_file "$incoming/.complete" || return 1
    names=$(cat "$incoming/.complete") || return 1
    case "$names" in installed|"installed
apps") ;; *) return 1 ;; esac
    for name in $names; do
        test ! -L "$incoming/$name" && test -d "$incoming/$name" || return 1
        test ! -L "$sites/$name" || return 1
        if test -e "$sites/$name"; then test -d "$sites/$name" || return 1; fi
        case "$name" in
            installed) marker="$incoming/installed/.dayframe-layout-managed" text=dayframe-layout-source-v1 ;;
            apps) marker="$incoming/apps/.dayframe-apps-managed" text=dayframe-apps-source-v1 ;;
        esac
        test ! -L "$marker" && test -f "$marker" && test "$(cat "$marker")" = "$text" || return 1
    done
    printf '%s\n' "$names"
}

# check_incoming: print the names of a complete transfer, or nothing when there is none.
# Removes a transfer that did not finish and says so. Fails on a transfer that is not valid.
check_incoming() {
    if ! test -e "$incoming" && ! test -L "$incoming"; then return 0; fi
    if test -L "$incoming" || ! test -f "$incoming/.complete"; then
        rm -rf "$incoming" || return 1
        echo 'Removed sites/.incoming: its transfer did not finish. The installed layouts stay as they are.' >&2
        return 0
    fi
    incoming_names || {
        echo 'sites/.incoming is not a valid layout transfer. Run the deploy again.' >&2
        return 1
    }
}

# place_incoming <folder>: move the transferred layouts into sites/. Each folder they replace
# moves to <folder>, which must not exist yet. <folder>/steps records each step before
# it is done, so restore_sites can undo exactly the steps that ran. Signals are ignored
# for the swap itself, so a lost connection can't stop it halfway.
place_incoming() {
    names=$(incoming_names) || {
        echo 'sites/.incoming is not a complete layout transfer. Nothing was moved.' >&2
        return 1
    }
    if test -e "$1" || test -L "$1"; then return 1; fi
    for name in $names; do
        if test -e "$1/$name"; then return 1; fi
    done
    mkdir -m 700 "$1" || return 1
    : > "$1/steps" || return 1
    trap '' HUP INT TERM
    if swap_in "$1" $names; then swap_status=0; else swap_status=1; fi
    # shellcheck disable=SC2064 # after_swap is a fixed action, set on purpose now.
    trap "$after_swap" HUP INT TERM
    test "$swap_status" = 0 || return 1
    rm -rf "$incoming" || true
}

swap_in() {
    folder=$1
    shift
    for name in "$@"; do
        if test -e "$sites/$name"; then
            echo "out $name" >> "$folder/steps" || return 1
            mv "$sites/$name" "$folder/$name" || return 1
        else
            echo "absent $name" >> "$folder/steps" || return 1
        fi
        echo "in $name" >> "$folder/steps" || return 1
        mv "$incoming/$name" "$sites/$name" || return 1
    done
    echo 'done' >> "$folder/steps" || return 1
}

# swap_state <folder>: none (no folder), empty (a folder with nothing in it), finished
# (a swap with its final "done" step and no undo), unfinished (a swap without "done", or
# whose undo started and did not finish; a finished undo removes the folder) or unknown
# (layout folders without a steps file, for example from an older Art Frames).
swap_state() {
    if ! test -e "$1" && ! test -L "$1"; then echo none; return 0; fi
    if test -L "$1" || ! test -d "$1"; then echo unknown; return 0; fi
    if ! test -f "$1/steps"; then
        if test -z "$(ls -A "$1")"; then echo empty; else echo unknown; fi
        return 0
    fi
    if grep -qx 'done' "$1/steps" && ! grep -qx 'undo' "$1/steps"; then echo finished; else echo unfinished; fi
}

# unfinished_swap <folder>: true when <folder> may hold the only copy of layout folders.
unfinished_swap() {
    case "$(swap_state "$1")" in unfinished|unknown) return 0 ;; esac
    return 1
}

unknown_sites() {
    echo "$1 holds layout folders but no steps file, so Art Frames can't tell which ones ran." \
        "Move the folders you need back to sites/ by hand, remove $1, then try again." >&2
}

# recover_sites <folder> <what to run next>: put back a swap that stopped halfway, for
# example after a crash. Sets recovered=1 when it moved a folder. Fails, with a message,
# when that is not possible. Leaves a folder it can't read alone.
recover_sites() {
    case "$(swap_state "$1")" in
        none|finished) return 0 ;;
        empty) rmdir "$1" || return 1; return 0 ;;
        unknown) unknown_sites "$1"; return 1 ;;
    esac
    sites_moved=""
    if restore_sites "$1"; then
        if test -n "$sites_moved"; then
            recovered=1
            echo 'Put back the layout folders of an earlier run that stopped halfway.' >&2
        fi
        return 0
    fi
    sites_not_restored "$1" "$2"
    return 1
}

# recover_update <what to run next>: when an update stopped before it started the new
# version ($saved/unfinished is there), put back the layout folders, .env and the override
# it saved, so they match the running version again. Otherwise recover_sites.
recover_update() {
    if ! test -f "$saved/unfinished"; then
        recover_sites "$saved/sites" "$1"
        return
    fi
    sites_moved=""
    case "$(swap_state "$saved/sites")" in
        unknown) unknown_sites "$saved/sites"; return 1 ;;
        empty) rmdir "$saved/sites" || return 1 ;;
        finished|unfinished)
            restore_sites "$saved/sites" || { sites_not_restored "$saved/sites" "$1"; return 1; } ;;
    esac
    restore_files || {
        echo "Could not put back .env and the layout override of an update that stopped. $1" >&2
        return 1
    }
    rm -f "$saved/unfinished" || return 1
    # shellcheck disable=SC2034 # place-apps.sh reads it.
    recovered=1
    if test -n "$sites_moved"; then
        echo 'Put back the layout folders, .env and the layout override of an update that stopped before it started the new version.' >&2
    else
        echo 'Put back .env and the layout override of an update that stopped before it started the new version.' >&2
    fi
}

sites_not_restored() {
    echo "Could not put back the layout folders. The steps are in $1/steps. $2" >&2
}

# restore_sites <folder>: undo the steps that place_incoming recorded in <folder>/steps.
# A name without a record stays as it is. A replaced folder comes back before the new
# one goes, so no step removes a folder that is not back in place.
restore_sites() {
    test -e "$1" || test -L "$1" || return 0
    test ! -L "$1" && test -d "$1" || return 1
    trap '' HUP INT TERM
    if undo_steps "$1"; then swap_status=0; else swap_status=1; fi
    # shellcheck disable=SC2064 # after_swap is a fixed action, set on purpose now.
    trap "$after_swap" HUP INT TERM
    return "$swap_status"
}

undo_steps() {
    folder=$1
    if ! test -f "$folder/steps"; then
        # Without steps nothing is known about the folders here. Remove only an empty folder.
        if test -z "$(ls -A "$folder")"; then rmdir "$folder" || return 1; return 0; fi
        unknown_sites "$folder"
        return 1
    fi
    echo 'undo' >> "$folder/steps" || return 1
    for name in apps installed; do
        test ! -L "$sites/$name" || return 1
        if grep -qx "out $name" "$folder/steps"; then
            # The replaced folder moved out. What is in sites/ now can only be the new one.
            test -d "$folder/$name" || continue
            if test -e "$sites/$name"; then mv "$sites/$name" "$folder/$name.new" || return 1; fi
            mv "$folder/$name" "$sites/$name" || return 1
            sites_moved=1
            rm -rf "${folder:?}/$name.new" || return 1
        elif grep -qx "absent $name" "$folder/steps" && grep -qx "in $name" "$folder/steps"; then
            # Nothing was there before, so a folder there now is the new one.
            if test -e "$sites/$name"; then
                rm -rf "${sites:?}/$name" || return 1
                sites_moved=1
            fi
        fi
    done
    rm -rf "$folder" || return 1
}

# save_settings: copy data/settings.json to the saved state. Run it with the controller
# stopped, so nothing can change or replace the file during the copy.
save_settings() {
    rm -f "$saved/settings.json" "$saved/saved-at" || return 1
    test ! -L "$data" || return 1
    if ! test -e "$data/settings.json" && ! test -L "$data/settings.json"; then return 0; fi
    plain_file "$data/settings.json" || {
        echo 'data/settings.json is not a plain file. Refusing to copy it.' >&2
        return 1
    }
    cp "$data/settings.json" "$saved/settings.json" || return 1
    chmod 600 "$saved/settings.json" || return 1
    date +%s > "$saved/saved-at" || return 1
}

# settings_replaceable: true when data/settings.json is missing or a plain file.
settings_replaceable() {
    test ! -L "$data" || return 1
    test ! -L "$data/settings.json" || return 1
    if test -e "$data/settings.json"; then test -f "$data/settings.json" || return 1; fi
}

# restore_settings: put the saved settings.json back, owned by the container user, and
# rename the saved copy so a second rollback does not restore it again.
# Run it with the controller stopped.
restore_settings() {
    settings_replaceable || {
        echo 'data/settings.json is not a plain file. Refusing to replace it.' >&2
        return 1
    }
    plain_file "$saved/settings.json" || return 1
    temp=$(mktemp "$data/.settings-restore.XXXXXX") || return 1
    if ! { cp "$saved/settings.json" "$temp" && chown -h 10001:10001 "$temp" && chmod 600 "$temp" \
        && mv "$temp" "$data/settings.json"; }; then
        rm -f "$temp"
        return 1
    fi
    mv "$saved/settings.json" "$saved/settings.restored-$(date +%Y%m%d-%H%M%S).json" || return 1
}

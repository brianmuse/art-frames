# The one-line .install-profile that install-layout.py writes (PROFILE_FIELDS there).
# Source this file: . scripts/app-profile.sh
# prepare-remote.sh reads the first layout's profile and app-compose.sh each extra layout's.

# read_profile <file>: set app_id, app_network, app_memory, app_entry, app_scope and
# app_artwork from the profile, and return 1 when any field is not valid.
# Each check ends in `|| return 1`, because a caller's `set -e` does not apply inside
# a function that runs as part of an `if` or `||` list.
read_profile() {
    test ! -L "$1" || return 1
    test -f "$1" || return 1
    read -r app_id app_network app_memory app_entry app_scope app_artwork < "$1" || return 1
    printf '%s' "$app_id" | grep -Eq '^[a-z][a-z0-9-]{0,63}$' || return 1
    printf '%s' "$app_scope" | grep -Eq '^[a-z][a-z0-9-]{0,95}$' || return 1
    case "$app_artwork" in 0|1) ;; *) return 1 ;; esac
    case "$app_network" in none|bridge) ;; *) return 1 ;; esac
    printf '%s' "$app_memory" | grep -Eq '^[0-9]{3,4}$' || return 1
    test "$app_memory" -ge 128 || return 1
    test "$app_memory" -le 1024 || return 1
    printf '%s' "$app_entry" | grep -Eq '^[A-Za-z0-9_/-]+\.html$' || return 1
    case "$app_entry" in /*|*..*) return 1 ;; esac
}

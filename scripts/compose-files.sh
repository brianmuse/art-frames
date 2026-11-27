# Shared Compose helpers. Source this file from the source folder: . scripts/compose-files.sh
# The admin scripts and remote.py all use it, so the renderer mode, the file list and the
# extra-layout list are read the same way everywhere.

# compose_mode <profile>: print the renderer mode of a shipped Compose profile, static or browser.
compose_mode() {
    case "$1" in
        compose.yaml|compose.host.yaml) printf '%s\n' browser ;;
        compose.static.yaml|compose.static.host.yaml) printf '%s\n' static ;;
        *) echo 'Choose a shipped Compose profile' >&2; return 1 ;;
    esac
}

# compose_files <profile>: print the -f options for a shipped Compose profile. A static
# profile adds sites/compose.apps.yaml, the override for extra layouts, when it exists.
compose_files() {
    case "$1" in
        compose.yaml|compose.host.yaml) printf '%s\n' "-f $1" ;;
        compose.static.yaml|compose.static.host.yaml)
            if test -f sites/compose.apps.yaml; then
                printf '%s\n' "-f $1 -f sites/compose.apps.yaml"
            else
                printf '%s\n' "-f $1"
            fi ;;
        *) echo 'Choose a shipped Compose profile' >&2; return 1 ;;
    esac
}

# extra_app_ids <profile>: print the ID of each extra layout in DAYFRAME_APPS ("<id>=<scope> ..."),
# one per line. Prints nothing for a browser profile or when there is no override.
extra_app_ids() {
    case "$1" in
        compose.static.yaml|compose.static.host.yaml) ;;
        *) return 0 ;;
    esac
    test -f sites/compose.apps.yaml || return 0
    test -f .env || return 0
    for pair in $(sed -n 's/^DAYFRAME_APPS=//p' .env | tail -n 1); do
        id=${pair%%=*}
        if ! printf '%s' "$id" | grep -Eq '^[a-z][a-z0-9-]{0,63}$'; then
            echo 'DAYFRAME_APPS in .env holds an invalid layout ID' >&2
            return 1
        fi
        printf '%s\n' "$id"
    done
}

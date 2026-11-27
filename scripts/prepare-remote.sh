#!/bin/sh
# Run through remote.py prepare. Changes only this managed installation's files.
# Usage: prepare-remote.sh static|browser (the renderer mode of the Compose profile).
# update-remote.sh can set DAYFRAME_FIRST_DIR and DAYFRAME_APPS_DIR to the transferred layout
# folders in sites/.incoming. The profiles are read from there; .env and the override
# still name the final folders, sites/installed and sites/apps.
set -eu
umask 077
mode=${1:-}
case "$mode" in
    static|browser) ;;
    *) echo 'Usage: prepare-remote.sh static|browser' >&2; exit 1 ;;
esac
test "$(id -u)" = 0 || { echo 'Run this preparation through sudo' >&2; exit 1; }
source_dir=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd -P)
base=$(dirname "$source_dir")
test "$(basename "$source_dir")" = source
test ! -L "$base/.dayframe-managed"
test "$(cat "$base/.dayframe-managed")" = dayframe-managed-source-v1
for name in data sites .secrets config.local.toml .env; do
    test ! -L "$base/$name" || { echo "Refusing symlinked runtime path: $name" >&2; exit 1; }
    if test -e "$base/$name"; then
        case "$name" in
            data|sites|.secrets) test -d "$base/$name" ;;
            *) test -f "$base/$name" ;;
        esac
    fi
    if test -e "$source_dir/$name" || test -L "$source_dir/$name"; then
        test -L "$source_dir/$name"
        test "$(readlink "$source_dir/$name")" = "../$name"
    fi
done
# Preserve all existing installation configuration, passwords, and data.
for name in data sites .secrets; do mkdir -p "$base/$name"; done
test -f "$base/config.local.toml" || cp "$source_dir/config.example.toml" "$base/config.local.toml"
test -f "$base/.env" || cp "$source_dir/.env.example" "$base/.env"
test ! -L "$base/.secrets/web_password"
if ! test -e "$base/.secrets/web_password"; then
    password_temp=$(mktemp "$base/.secrets/.password.XXXXXX")
    trap 'rm -f "$password_temp"' EXIT
    trap 'exit 1' HUP INT TERM
    openssl rand -base64 32 > "$password_temp"
    test "$(wc -c < "$password_temp")" -ge 17
    mv "$password_temp" "$base/.secrets/web_password"
fi
test -f "$base/.secrets/web_password"
test "$(wc -c < "$base/.secrets/web_password")" -ge 16 || {
    echo 'Existing management password is invalid; replace it locally before continuing' >&2
    exit 1
}
# Existing custom UID/GID installations need their own ownership setup.
grep -qx 'DAYFRAME_UID=10001' "$base/.env"
grep -qx 'DAYFRAME_GID=10001' "$base/.env"
# rewrite_env <awk program> [printf format and arguments]: keep the .env lines that the
# program prints, then append the printf text. The owner and mode of .env stay the same.
rewrite_env() {
    env_filter=$1
    shift
    env_owner=$(ls -nd "$base/.env" | awk '{ print $3 ":" $4 }')
    env_temp=$(mktemp "$base/.environment.XXXXXX")
    awk "$env_filter" "$base/.env" > "$env_temp"
    if test "$#" -gt 0; then
        # The format is always one of the fixed strings below.
        # shellcheck disable=SC2059
        printf "$@" >> "$env_temp"
    fi
    chown "$env_owner" "$env_temp"
    chmod 600 "$env_temp"
    mv "$env_temp" "$base/.env"
}
app_keys='/^DAYFRAME_LAYOUT_(ID|ENTRY|MANIFEST|CONFIG_ID|ARTWORK)=/ || /^DAYFRAME_SITE_(BUILD_CONTEXT|DOCKERFILE|NETWORK|MEMORY)=/'
. "$source_dir/scripts/app-profile.sh"
first_dir=${DAYFRAME_FIRST_DIR:-$base/sites/installed}
apps_dir=${DAYFRAME_APPS_DIR:-$base/sites/apps}
for dir in "$first_dir" "$apps_dir"; do
    case "$dir" in "$base/sites/installed"|"$base/sites/apps"|"$base/sites/.incoming/installed"|"$base/sites/.incoming/apps") ;;
        *) echo 'The layout folders must be in sites/ or sites/.incoming' >&2; exit 1 ;;
    esac
done
profile="$first_dir/.install-profile"
if test "$mode" = browser; then
    # A browser profile runs no layouts: remove the layout keys, the override and DAYFRAME_APPS.
    # Art Frames then shows its built-in layout. The installed layout files stay for a static profile.
    extra_apps=""
    test ! -L "$base/sites/compose.apps.yaml"
    rm -f "$base/sites/compose.apps.yaml"
    if grep -Eq '^DAYFRAME_(APPS|LAYOUT_[A-Z_]+|SITE_[A-Z_]+)=' "$base/.env"; then
        rewrite_env "!($app_keys || /^DAYFRAME_APPS=/)"
    fi
else
    # The first layout is transferred separately from the core's committed source.
    if test -f "$profile" || test -L "$profile"; then
        test ! -L "$first_dir"
        test ! -L "$first_dir/.dayframe-layout-managed"
        test "$(cat "$first_dir/.dayframe-layout-managed")" = dayframe-layout-source-v1
        read_profile "$profile" || { echo 'The profile of the first layout is not valid' >&2; exit 1; }
        rewrite_env "!($app_keys)" \
            '\nDAYFRAME_LAYOUT_ID=%s\nDAYFRAME_LAYOUT_ENTRY=%s\nDAYFRAME_LAYOUT_CONFIG_ID=%s\nDAYFRAME_LAYOUT_ARTWORK=%s\nDAYFRAME_LAYOUT_MANIFEST=/sites/installed/layout.json\nDAYFRAME_SITE_BUILD_CONTEXT=./sites/installed\nDAYFRAME_SITE_DOCKERFILE=Dockerfile\nDAYFRAME_SITE_NETWORK=%s\nDAYFRAME_SITE_MEMORY=%sm\n' \
            "$app_id" "$app_entry" "$app_scope" "$app_artwork" "$app_network" "$app_memory"
    fi
    # Extra layouts in sites/apps: the shared generator validates each profile and writes
    # sites/compose.apps.yaml (or removes it when there are none).
    extra_apps=$(sh "$source_dir/scripts/app-compose.sh" "$base" "$apps_dir")
    saved_apps=$(sed -n 's/^DAYFRAME_APPS=//p' "$base/.env" | tail -n 1)
    if test "$extra_apps" != "$saved_apps"; then
        rewrite_env '!/^DAYFRAME_APPS=/' 'DAYFRAME_APPS=%s\n' "$extra_apps"
    fi
fi
test ! -L "$base/data/layout-settings"
layout_id=$(sed -n 's/^DAYFRAME_LAYOUT_CONFIG_ID=//p' "$base/.env" | tail -n 1)
layout_id=${layout_id:-starter}
printf '%s' "$layout_id" | grep -Eq '^[a-z][a-z0-9-]{0,95}$'
# A layout whose source was renamed (for example, its GitHub repository) gets its saved
# settings back from its scope under the earlier name. This runs before the folders below
# are made, and it never replaces saved settings. A failure here does not stop the update.
if test "$mode" = static; then
    set -- "$first_dir"
    for pair in $extra_apps; do set -- "$@" "$apps_dir/${pair%%=*}"; done
    sh "$source_dir/scripts/keep-layout-settings.sh" "$base/data" "$@" ||
        echo 'Could not restore the saved settings of a layout from an earlier install. Check the layout pages.' >&2
fi
test ! -L "$base/data/layout-settings/$layout_id"
mkdir -p "$base/data/layout-settings/$layout_id"
chown 10001:10001 "$base/data/layout-settings" "$base/data/layout-settings/$layout_id"
chmod 700 "$base/data/layout-settings" "$base/data/layout-settings/$layout_id"
test ! -L "$base/data/layout-state"
test ! -L "$base/data/layout-state/$layout_id"
mkdir -p "$base/data/layout-state/$layout_id"
chown 10001:10001 "$base/data/layout-state" "$base/data/layout-state/$layout_id"
chmod 700 "$base/data/layout-state" "$base/data/layout-state/$layout_id"
for runtime_file in "$base/data/settings.json" "$base/data/legacy-dashboard.json" "$base/data/layout-settings/$layout_id/config.json"; do
    test ! -L "$runtime_file"
    if test -e "$runtime_file"; then
        test -f "$runtime_file"
        chown 10001:10001 "$runtime_file"
        chmod 600 "$runtime_file"
    fi
done
for pair in $extra_apps; do
    extra_scope=${pair#*=}
    printf '%s' "$extra_scope" | grep -Eq '^[a-z][a-z0-9-]{0,95}$'
    for kind in layout-settings layout-state; do
        test ! -L "$base/data/$kind/$extra_scope"
        mkdir -p "$base/data/$kind/$extra_scope"
        chown 10001:10001 "$base/data/$kind/$extra_scope"
        chmod 700 "$base/data/$kind/$extra_scope"
    done
    runtime_file="$base/data/layout-settings/$extra_scope/config.json"
    test ! -L "$runtime_file"
    if test -e "$runtime_file"; then
        test -f "$runtime_file"
        chown 10001:10001 "$runtime_file"
        chmod 600 "$runtime_file"
    fi
done
chown 10001:10001 "$base/data" "$base/.secrets" "$base/.secrets/web_password" "$base/config.local.toml"
chown "$(ls -nd "$base" | awk '{ print $3 ":" $4 }')" "$base/sites"
chmod 700 "$base/data" "$base/.secrets"
chmod 755 "$base/sites"
chmod 600 "$base/.secrets/web_password" "$base/config.local.toml" "$base/.env"
for name in data sites .secrets config.local.toml .env; do
    test -L "$source_dir/$name" || ln -s "../$name" "$source_dir/$name"
done
echo 'Prepared app-owned runtime directories. Existing settings and passwords were preserved.'
echo 'Management password is in the private .secrets/web_password file; do not paste it into chat.'

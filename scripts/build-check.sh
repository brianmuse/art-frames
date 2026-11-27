#!/bin/sh
# Check the host, build every image and check each one before anything is activated.
# Usage: sh scripts/build-check.sh <compose profile>
# update-remote.sh and remote.py up both run it, so a broken layout image stops either here.
set -eu
compose=${1:-}
cd "$(dirname "$0")/.."
. scripts/compose-files.sh
mode=$(compose_mode "$compose")
renderer=dayframe-renderer
test "$mode" = browser || renderer=dayframe-renderer-static
files=$(compose_files "$compose")
extra=$(extra_app_ids "$compose")
sh scripts/check-host.sh "$mode"
# $files holds only a validated profile name and the fixed override path.
# shellcheck disable=SC2086
docker compose -p dayframe $files build
sh scripts/check-containers.sh dayframe:local "$renderer:local" "$mode" dayframe-website:local
for id in $extra; do
    echo "Checking the $id layout image…"
    sh scripts/check-containers.sh dayframe:local "$renderer:local" "$mode" "dayframe-app-$id:local"
done

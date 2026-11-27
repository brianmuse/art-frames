#!/bin/sh
# Wait up to 60 seconds for Art Frames to report healthy from inside its container.
set -eu
compose=$1
. "$(dirname "$0")/compose-files.sh"
files=$(compose_files "$compose")
attempt=0
while test "$attempt" -lt 30; do
    # $files holds only a validated profile name and the fixed override path.
    # shellcheck disable=SC2086
    if docker compose -p dayframe $files exec -T dayframe python -c \
        "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('DAYFRAME_PORT','8765')+'/health',timeout=2)" \
        </dev/null >/dev/null 2>&1; then
        echo 'Art Frames is healthy.'
        exit 0
    fi
    attempt=$((attempt + 1))
    sleep 2
done
echo 'Art Frames did not become healthy.' >&2
exit 1

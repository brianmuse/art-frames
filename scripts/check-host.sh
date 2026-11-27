#!/bin/sh
# Read daemon capabilities before building images or creating test resources.
set -eu
mode=${1:-browser}
case "$mode" in browser|static) ;; *) echo 'Unknown renderer mode' >&2; exit 1 ;; esac
capabilities=$(docker info --format '{{range .SecurityOptions}}{{println .}}{{end}}pids={{.PidsLimit}} memory={{.MemoryLimit}}')
case "$capabilities" in
    *memory=true*) ;;
    *) echo 'Art Frames requires a Docker host that can enforce memory limits.' >&2; exit 1 ;;
esac
if test "$mode" = static; then
    echo 'HTML/CSS renderer selected: no browser JavaScript. Memory and per-process limits apply.'
    exit 0
fi
case "$capabilities" in
    *name=seccomp*) ;;
    *)
        echo 'Art Frames cannot run its sandboxed browser on this Docker host: seccomp is unavailable.' >&2
        echo 'Use a Linux VM with seccomp support or another compatible Docker host.' >&2
        echo 'Changing the Compose file cannot add a missing kernel feature.' >&2
        exit 1
        ;;
esac
case "$capabilities" in
    *pids=true*) ;;
    *)
        echo 'Art Frames requires process limits, but this Docker host cannot enforce them.' >&2
        echo 'Use a Linux VM with PID cgroup support or another compatible Docker host.' >&2
        exit 1
        ;;
esac
echo 'Host supports seccomp and process limits; a real sandboxed render must still pass.'

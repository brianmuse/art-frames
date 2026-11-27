# Compatibility

This page lists what is known to work. It is not a guarantee. To add your results, open an issue with the **TV compatibility report** form.

## TVs

| TV | Status |
| --- | --- |
| Samsung The Frame | The author runs Art Frames on his own Frame TVs. The exact models and firmware are not recorded here yet. |
| Other Samsung TVs | TV search finds them through Samsung's device-info endpoint. Art Mode needs a Frame. |

Wake and sleep behaviour can differ between models and firmware. Test a scheduled wake on your TV before you rely on it.

## Hosts

| Host | Status |
| --- | --- |
| Synology DS920+ (kernel 4.4.302+) | The install file works with the HTML/CSS renderer: the starter website, a live Astro dashboard and artwork. The Chromium renderer does not start, because the kernel has no seccomp and no PID cgroup support. |
| Docker Desktop on macOS (Apple Silicon) | Both renderers make 4K images, with native arm64 and emulated amd64 images. Docker Desktop has no host network, so TV search and wake do not work. See the [README](../README.md#docker-desktop-on-a-mac-or-windows-pc). |
| Linux on amd64 and arm64 | Each release builds the images for both. CI runs the container checks on Linux amd64. |

The images have no 32-bit build.

## Check your own host

After you build the images from source, run `sh scripts/check-host.sh` and `sh scripts/check-containers.sh APP_IMAGE RENDERER_IMAGE`. The checks use test data and do not contact a TV. See [renderer choices](renderers.md#validation).

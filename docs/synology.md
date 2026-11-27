# Synology installation

Use DSM with **Container Manager**, on the same LAN as the TV. Art Frames has no reason to read your photo, document, backup, media, or other private shares.

## Simplest install: Container Manager project

This path uses the published images and needs no source, no password file and no config file. It uses the HTML/CSS renderer, so it works on kernels that can't run the Chromium renderer. It is tested on a DS920+.

1. In **File Station**, create a folder for the project, for example `docker/art-frames`.
2. Put `install/docker-compose.yml` from this repository in that folder.
3. In **Container Manager → Project → Create**, name the project `art-frames`. Choose that folder as the path and select the existing `docker-compose.yml`. Start the project.
4. Open **Container Manager → Container → art-frames → Log**. Find the setup link and the address.
5. Open the setup link on a computer or phone on your home network. Create the household password.

### Where the data is

The project keeps everything in the Docker volume `art-frames_data`. Name the project `art-frames`, so the volume has that name. We chose a named volume because it works with no other step: Docker gives an empty volume the right owner (user 10001). A shared folder would need that owner too, and DSM can't set it in File Station.

Hyper Backup does not see Docker volumes. Back up in one of these ways:

- **Settings:** **Server → Export settings → Download backup** in Art Frames, with **Include private values** ticked. Keep the file in a shared folder that Hyper Backup copies. Do this before each update.
- **Everything, in a shared folder** (optional): store the data in a shared folder instead. This needs one root command, which DSM's Task Scheduler can run with no terminal:
  1. In File Station, make `docker/art-frames/data`.
  2. Open **Control Panel → Task Scheduler → Create → Triggered Task → User-defined script**. Set the user to `root` and the event to **Boot-up**. Under **Task Settings**, type `chown -R 10001:10001 /volume1/docker/art-frames/data` (change `volume1` if your folder is on another volume).
  3. Select the task and **Run** it once.
  4. In the project's `docker-compose.yml`, change `- data:/data` to `- /volume1/docker/art-frames/data:/data`. Then build the project again (see below).

### Update

Use these steps for every update:

1. In Art Frames, open **Server → Export settings** and download a backup with **Include private values**.
2. In **Container Manager → Project**, select `art-frames`, then **Action → Stop**.
3. Select **Action → Build**. Container Manager pulls the newest images and starts the project again.
4. Open Art Frames. The foot of the sidebar names the version.

To go back to an older version, set its number in the `image:` lines (for example `:0.1` instead of `:latest`), build again, and import the backup. An older version may not read settings that a newer one wrote.

See [Hosting](hosting.md#access-and-networking) for plain-HTTP limits and HTTPS.

## Why the HTML/CSS renderer

The tested DS920+ kernel `4.4.302+` rejects custom seccomp profiles and cannot enforce cgroup PID limits. The Chromium renderer refuses to start on such a kernel. This is a limit of the host, not a failed image build. Do not remove the browser's sandbox settings to get around it. The install file uses the HTML/CSS renderer, which needs neither feature. See [renderer choices](renderers.md).

To build Art Frames from source on a Synology, see [deployment from your computer](cli-deployment.md#synology-source-install).

## Convenient HTTPS access

You may use DSM's reverse proxy with a valid certificate and a hostname reachable only over your LAN/VPN. Keep its destination at `127.0.0.1:8765`. Add that hostname to `ART_FRAMES_HOSTS` and set `ART_FRAMES_SECURE_COOKIES=1` (the old `DAYFRAME_` names still work). With the install file, put these under `environment:` for the `art-frames` service: `ART_FRAMES_BIND: 127.0.0.1`, `ART_FRAMES_SECURE_COOKIES: "1"` and `ART_FRAMES_HOSTS: "*,<hostname>"`. The bind keeps plain HTTP off the network, so the secure cookie is not sent in the clear. Every browser then reaches Art Frames from 127.0.0.1, so all of them share one sign-in limit (5 wrong tries a minute). Keep Art Frames' own password enabled. Do not forward the port through the router.

## Other administration

DSM itself and unrelated packages are administered separately from Art Frames. Installing Art Frames does not require changing their settings or reading their data. Model-specific compatibility results belong in the project's issue template, never in a dump of private NAS configuration.

# Hosting

Art Frames uses a controller/management service and an isolated renderer. HTML/CSS deployments also include an optional Astro service for the default website's dynamic content. It has no Synology-specific application paths, accounts, APIs, or database-server requirements.

With the install file (published images):

| Host | Deployment |
| --- | --- |
| Any Docker host, NAS or mini PC (simplest) | `install/docker-compose.yml`: published images, HTML/CSS renderer, host network, LAN HTTP, first-run setup in the browser |
| Docker Desktop on a Mac or Windows PC | `install/docker-compose.yml` plus `install/docker-compose.bridge.yml` |
| Synology Container Manager | `install/docker-compose.yml`; see [Synology installation](synology.md) |

From source:

| Host | Deployment |
| --- | --- |
| Linux server or mini PC | `compose.yaml`; optional `compose.host.yaml` for LAN wake broadcasts |
| Synology over SSH | An HTML/CSS profile on kernels without seccomp; see [deployment from your computer](cli-deployment.md) |
| Another Docker-capable NAS | The same services; mount only Art Frames' own folders |
| Docker Desktop | The default bridge Compose file; host broadcasts depend on platform support |
| Another orchestrator | Keep the mounts, credentials, private socket and network boundaries |

## Source Compose profiles

The source install uses standard Docker Compose:

- The default profile (`compose.yaml`) uses a sandboxed Chromium renderer, for sites that need browser JavaScript.
- `compose.static.yaml` and `compose.static.host.yaml` use the lightweight HTML/CSS renderer plus a website server. Use one of these for an installed layout with its own server.
- Host profiles give only the controller the host network, for TV control and Wake-on-LAN.

In these source profiles, HTTP stays bound to loopback. Only `install/docker-compose.yml` binds to the home network.

Create the private runtime configuration and a strong management password as described below, or with [the SSH helper](cli-deployment.md). Keep UID/GID 10001 as the owner of the Art Frames data. `remote.py prepare` does this for the managed NAS paths. Mount only Art Frames' own files. Never mount unrelated NAS shares or the Docker socket into any Art Frames container.

For a Linux source install, run these commands in the source folder. They use the same owners and modes as `remote.py prepare`.

```sh
umask 077
cp config.example.toml config.local.toml
cp .env.example .env
mkdir -p data sites .secrets
openssl rand -base64 32 > .secrets/web_password
chmod 755 sites
chmod 600 .env config.local.toml .secrets/web_password
sudo chown 10001:10001 data .secrets .secrets/web_password config.local.toml
```

Keep `.env` owned by your own user, because Docker Compose reads it. Keep `DAYFRAME_UID=10001` and `DAYFRAME_GID=10001` in `.env`. The password file must hold at least 16 characters.

The renderer never has a network, and it has no credential, config or data mounts. The bundled starter website server also has no network. A trusted installed layout can ask for a bridge network for its own API calls. It gets its own read-only configuration folder and a private writable state folder (`data/layout-state/<layout scope>`), but not the TV tokens or the management password. A layout can answer HTTP 202 while it prepares data. The preview button then tries again and keeps the previous image until the layout is ready.

## Storage

The install file (`install/docker-compose.yml`) keeps everything in one named volume, mounted at `/data`, except the layouts' data. Each layout has three small volumes of its own: `layout-<name>-settings` (its settings, mounted in Art Frames at `/data/layout-settings/<name>` and read-only in the layout at `/site-config`), `layout-<name>-state` (its saved downloads, at `/site-state`) and `layout-<name>-ipc` (its socket). So a layout never sees the TV pairings, the password or another layout's settings. It needs no password file, no config file and no `sites` folder. Docker names the volume after the project: `art-frames_data` when the project is `art-frames`. The image creates `/data` owned by UID/GID 10001, so an empty named volume gets the right owner.

A host folder also works, and NAS backup tools can then see it. Give it to user 10001 first: `sudo chown -R 10001:10001 <folder>`. Then change `- data:/data` to `- <folder>:/data`. The [README](../README.md#unraid) shows this for Unraid, and [Synology](synology.md#where-the-data-is) shows it without a terminal. Art Frames stops at start with a clear message when it can't write `/data`.

Files the sign-in and update features add to the data folder:

- `.session-secret`: the key that signs sign-in cookies. It stays across restarts and updates. Keep the data volume, or every device must sign in again.
- `auth.json`: the household password as a scrypt hash, when you created it in the browser. To make a new password, use `ART_FRAMES_RESET_PASSWORD` (below).
- `auth-before-reset.json`: the old `auth.json`, kept after a password reset.
- `setup-code`: the first-run setup code. Art Frames deletes it when the password exists.
- `sessions.json`: the "Sign out everywhere" counter, for an install with a password file.
- `updates.json`: the update check switch and the last answer.

The source Compose files use these paths:

- `data/`: writable controller runtime files, catalog, images, settings, and per-device tokens.
- `config.local.toml`: private installation defaults, read-only to the controller.
- `.secrets/web_password`: strong management password, mounted as a read-only secret.
- `sites/`: your compiled websites, read-only to the controller and never mounted in the browser.
- `render-ipc`: a named Docker volume containing only the renderer socket and process lock; read-only to the controller, writable to the worker.
- `site-ipc`: in HTML/CSS example deployments, a separate socket-only volume for the isolated Astro service; read-only to the controller.

The controller defaults to UID/GID 10001, configurable through `.env`. It also joins the private socket's GID 10001 inside the container; no host group membership changes are needed. The worker uses UID/GID 10001, **network_mode: none**, no NAS/data/config/credential mounts, and no Docker socket. Neither service is privileged.

## Access and networking

### The install file: plain HTTP on the home network

`install/docker-compose.yml` sets `ART_FRAMES_BIND: 0.0.0.0`, so phones and computers on your home network reach Art Frames at `http://<server IP>:8765`. The traffic is plain HTTP. A person on the same network can read it, including the password when you sign in. This suits a trusted home network. Do not forward port 8765 from your router. For HTTPS, use a reverse proxy (below).

### The source Compose files: loopback only

The source profiles (`compose*.yaml`) publish HTTP only on host 127.0.0.1. Use an SSH tunnel or a local HTTPS reverse proxy. Do not forward the service through your router. The renderer exposes no TCP port.

### Where a setting goes

With the install file, two places hold settings:

- **`environment:`** for the `art-frames` service in `docker-compose.yml`. Art Frames reads only what is listed here. Add a line with the same indent as the others, for example `ART_FRAMES_MDNS: "0"`. Put a number or `*` in quotes.
- **A `.env` file** next to `docker-compose.yml`. Docker Compose reads it to fill in `${...}` in the compose file, and does not pass it to Art Frames. In the install file only `ART_FRAMES_VERSION`, `ART_FRAMES_PORT` and `TZ` come from `.env`. Portainer calls this **Environment variables** on the stack page. Synology Container Manager and Unraid's Compose Manager have no such page: edit the compose file.

After a change, start the project again (`docker compose up -d`).

### Settings

The install file already sets the first group. Do not change those unless the note says so.

| Setting | Where | Default | What it does |
| --- | --- | --- | --- |
| `ART_FRAMES_VERSION` | `.env` | `latest` | The image version of Art Frames and its renderer, for example `0.1`. |
| `ART_FRAMES_LAYOUTS_VERSION` | `.env` | `latest` | The image version of the layouts, for example `0.1`. The layouts repository has its own releases. |
| `ART_FRAMES_PORT` | `.env` | `8765` | The port. The install file passes it to the container, and the image health check reads it. |
| `TZ` | `.env` | `UTC` | The time zone of the log. Set the household time zone in Art Frames. |
| `ART_FRAMES_BIND` | `environment:` (set) | `127.0.0.1` | The address Art Frames listens on. The install file sets `0.0.0.0` (every address). Behind a reverse proxy, set `127.0.0.1`. |
| `ART_FRAMES_DATA_DIR` | `environment:` (set) | `/data` | The data folder inside the container. |
| `ART_FRAMES_RENDERER_SOCKET`, `ART_FRAMES_DEFAULT_SITE_SOCKET` | `environment:` (set) | — | The private sockets of the renderer and the first layout. |
| `ART_FRAMES_LAYOUT_MANIFEST`, `ART_FRAMES_LAYOUT_ID`, `ART_FRAMES_LAYOUT_CONFIG_ID` | `environment:` (set) | — | The first layout: `morning`. Its image copies its `layout.json` next to its socket. |
| `ART_FRAMES_APPS`, `ART_FRAMES_SITES_DIR`, `ART_FRAMES_APPS_SOCKET_DIR` | `environment:` (set) | — | The other layouts, `news` and `weather`, and the folder that holds their sockets and `layout.json` files. |
| `ART_FRAMES_LOG_LEVEL` | `environment:` (set) | `INFO` | `DEBUG` shows more in the log. |
| `ART_FRAMES_HOSTS` | `environment:` | `localhost,127.0.0.1,[::1]`; `*` when the bind is not loopback | The host names Art Frames answers to. `*` means any home network name: a private IP address, a name that ends in `.local`, or a name with no dots. Add other names after a comma, for example `"*,frames.home.arpa"`. Other names get "Bad Request". |
| `ART_FRAMES_SECURE_COOKIES` | `environment:` | `0` | `1` marks the sign-in cookie Secure. Use it only behind HTTPS, together with `ART_FRAMES_BIND: 127.0.0.1`. Art Frames logs a warning when it is `1` and the bind is not loopback. |
| `ART_FRAMES_MDNS` | `environment:` | `auto` | `auto` announces `art-frames.local` when it can work (below). `1` forces it on, `0` turns it off. |
| `ART_FRAMES_MDNS_NAME` | `environment:` | `art-frames` | The `.local` name. Give each install on one network its own name. |
| `ART_FRAMES_UPDATE_CHECK` | `environment:` | not set | `1` or `0` overrides the switch on the Server page. |
| `ART_FRAMES_RESET_PASSWORD` | `environment:` | not set | `1` resets the password at start (below). Remove it afterwards. |
| `ART_FRAMES_PASSWORD_FILE` | `environment:` | not set | A file with the password (at least 16 characters). With it, Art Frames never shows first-run setup. |

Each setting also works with the old `DAYFRAME_` prefix. Art Frames reads `ART_FRAMES_` first. The image health check reads `ART_FRAMES_PORT`, then `DAYFRAME_PORT`.

### Sign-in

- Without a password file and without `auth.json`, Art Frames starts in setup mode. It prints a setup link with the setup code in the log and writes the code to `setup-code`. When the log is a terminal, it also prints a QR code. Every page opens the setup form. The code works for 1 hour. A try after that hour prints a new code. A restart also makes a new code.
- A sign-in lasts 30 days after the last visit on that device. Each visit sends a fresh cookie.
- After 5 wrong passwords in a minute from one client address, that address waits until the minute ends. Behind a reverse proxy, a tunnel or Docker Desktop's bridge network, every device reaches Art Frames from the same address, so they all share one limit. A wrong guess on one device can then make the others wait a minute.
- **Server → Password and sign-in** has **Sign out** and **Sign out everywhere**. A new password also signs out every other device.
- Scripts can still send HTTP Basic auth (user `art-frames` or `dayframe`). Art Frames never asks for it, so browsers show the sign-in form. After **Sign out**, that browser must use the form again, even if it saved a Basic password.

### Reset a forgotten password

1. Add `ART_FRAMES_RESET_PASSWORD: "1"` under `environment:` for the `art-frames` service.
2. Start Art Frames again. It keeps the old `auth.json` as `auth-before-reset.json` and goes back to first-run setup. Your TVs and settings stay.
3. Open the setup link from the log and create a new password.
4. Remove the line and start Art Frames again. Each start with the line set resets the password once more. The log reminds you.

Anyone who can read the container log can then set the password, the same as on the first start. With a password file (`ART_FRAMES_PASSWORD_FILE`, or `.secrets/web_password` in the source install), the reset does nothing, and the log says so. Change the file instead.

If `auth.json` is damaged, pages say so and point to this reset.

### HTTPS with a reverse proxy

Put a reverse proxy with a certificate in front of Art Frames, for example [Caddy](https://caddyserver.com/). Run the proxy on the same machine. With Docker, give the Caddy container the host network (`network_mode: host`), so `127.0.0.1:8765` is Art Frames and not the Caddy container.

```text
frames.home.arpa {
    tls internal
    reverse_proxy 127.0.0.1:8765
}
```

`tls internal` makes Caddy sign its own certificate. Each phone and computer must then trust Caddy's root certificate, or the browser shows a warning. Caddy's docs explain how to install it. A real certificate for a domain you own needs no such step.

Then add these lines under `environment:` for the `art-frames` service:

```yaml
      ART_FRAMES_BIND: 127.0.0.1
      ART_FRAMES_HOSTS: "*,frames.home.arpa"
      ART_FRAMES_SECURE_COOKIES: "1"
```

The bind is required: without it, plain HTTP stays open on the home network next to HTTPS. Art Frames logs a warning when `ART_FRAMES_SECURE_COOKIES` is `1` and the bind is not loopback. Behind the proxy, every browser reaches Art Frames from 127.0.0.1, so they share one sign-in limit (see Sign-in). On Synology, DSM's reverse proxy works the same way (see [Synology](synology.md#convenient-https-access)).

### art-frames.local (mDNS)

With host networking, Art Frames announces `art-frames.local` and an "Art Frames" web service with python-zeroconf. The log names the name it registered. It skips mDNS and logs why when:

- it listens only on loopback, or
- it runs in a Docker bridge network. There, multicast stays inside the Docker network, so no phone on your home network sees the name. Docker Desktop is always in this case.

Two installs on one network: give each its own name with `ART_FRAMES_MDNS_NAME`, for example `kitchen-frames`. Without that, mDNS gives the second install another name, and the log warns which name it got.

Status: the name and the service answered on a loopback test with python-zeroconf. It is not yet tested on a real home network or on a Synology, which runs its own mDNS service (Avahi) on the same port. If the name does not open, use the IP address. Guest networks and some mesh Wi-Fi systems block mDNS.

### TV network access

Normal TV image delivery can use bridge networking. TV search, finding a TV whose address changed, sensor search and Wake-on-LAN need the host network. The optional Linux host-network deployment gives only the controller that access. The browser stays networkless and uses the same private socket in either deployment.

## Browser sandbox

Renderer capabilities are checked **before** building. The Chromium deployment requires seccomp, PID cgroups, and enforced memory limits. Some Synology kernels do not provide the first two. Use the explicit HTML/CSS deployment described in [renderer choices](renderers.md), or a compatible Linux host. The HTML/CSS mode has different content and isolation capabilities; it is not an automatic browser fallback.

Chromium's sandbox is enabled with a profile based on Playwright's published seccomp profile for user namespaces. Art Frames additionally allows the `chroot` syscall needed by Chromium's sandbox in its own user namespace; all container capabilities remain dropped. A host kernel that disables required namespace support may refuse startup. Verify an actual capture on your host. Do not resolve a failure using privileged mode, SYS_ADMIN, or host-data mounts; use a host supporting the sandbox.

Source Docker builds support the platform architectures available to the pinned Python, Node, and Playwright dependencies. Both amd64 images build for the current Synology target, and local sandboxed container captures pass with native arm64 and emulated amd64 renderers. Validate a capture on your target host too; see [compatibility](compatibility.md).

## Updates

With the install file:

1. Download a backup first: **Server → Export settings → Download backup**, with **Include private values**.
2. Run `docker compose pull`, then `docker compose up -d`. For Synology, see [the update steps](synology.md#update).

To stay on one version, set `ART_FRAMES_VERSION` (for example `0.1`) in a `.env` file next to the compose file. To go back to an older version, restore the data backup too: an older version may not read settings that a newer one wrote. Each container keeps at most three 10 MB log files.

From source: review release notes, stop Art Frames, copy its private runtime files consistently, rebuild, and start.

### The update check

Art Frames never updates itself. When the check is on, it sends one request at most once a day: a `GET` to `https://api.github.com/repos/brianmuse/art-frames/releases/latest` with the header `User-Agent: Art-Frames`. It sends no ID, no version, no settings and no data about your TVs. It compares the answer with its own version and shows "Art Frames X is out" in the sidebar and on the Server page, where X is the new version. A failed check is quiet; it tries again the next day. When the repository has no release yet (GitHub answers 404), Art Frames counts that as "no release yet", not as a failure.

- A new install (no password file): the check is on. Turn it off in **Server → Updates**.
- An install with a password file, as installs from before this feature run: the check is off until you turn it on.
- `ART_FRAMES_UPDATE_CHECK=1` or `0` overrides the switch.

### Backups

**Server → Export settings → Download backup** saves a `.zip` with the settings of every TV and each layout's settings. By default it leaves out private values and the TVs' MAC addresses. It still holds the TVs' names and IP addresses. Tick **Include private values** to add the TV pairing tokens, the sensor keys, layout values of type "secret" and `legacy-dashboard.json`. The password, the session key and the artwork are never in the file. **Server → Import settings → Import backup** checks the whole file first, keeps the current settings as `settings.before-import-<time>.json`, then writes. A private value or a MAC address the backup leaves out keeps its current value. For a full copy, back up the whole data volume while Art Frames is stopped. With the install file, also back up the `layout-<name>-settings` volumes; the downloaded backup already holds their settings.

`/health` returns 200 when Art Frames is up. It returns 503 when the scheduler has stopped or the renderer socket does not accept a connection. The body never contains details; the Art Frames log names the failed part. Docker’s health check and the deploy check both use it. Dependency locks pin Python and Astro dependencies. NAS OS upgrades remain separate administration work. Startup after the wake grace window does not trigger a late wake.

# Security and private data

Art Frames is made for a trusted home network. It is an early project, not an audited internet-facing service.

## Report a vulnerability

Report a suspected vulnerability privately. Do not open a public issue.

1. Open the **Security** tab of the [GitHub repository](https://github.com/brianmuse/art-frames/security).
2. Select **Report a vulnerability**.
3. Describe the problem and how to reproduce it.

A problem in a layout from [art-frames-layouts](https://github.com/brianmuse/art-frames-layouts) goes to that repository. Report it the same way, from its **Security** tab.

Do not include live credentials, calendar feeds, TV tokens, IP addresses or private artwork in any report or issue.

## Defaults and boundaries

- By default the HTTP server binds to loopback. The install file (`install/docker-compose.yml`) binds to the home network, so other devices can open it. Demo can run without a password only on loopback and sends no real TV commands.
- Every page except `/health` and the sign-in assets needs the household password. The password comes from a password file (at least 16 characters) or from `auth.json`, which holds a scrypt hash of a password made in the browser (at least 12 characters).
- A new install without a password starts in setup mode. Only a person with the setup code can create the password. Art Frames prints the code in the container log and writes it to the data folder. The code works for 1 hour; a restart makes a new one. Anyone who can read the container log or the data folder can set the password first. `ART_FRAMES_RESET_PASSWORD=1` puts an install with a stored password back into setup mode at start, with a new code in the log, so log access is also what a password reset trusts. It never touches an install with a password file.
- After 5 wrong passwords or setup codes in a minute, a client address must wait. This counts HTTP Basic tries too. Behind a reverse proxy, a tunnel or Docker Desktop's bridge network, every client has the same address, so they share one limit; Art Frames does not read `X-Forwarded-For`. **Sign out everywhere** and a new password end the sign-in on every other device. A sign-in lasts 30 days after the last visit.
- The install file serves plain HTTP on the home network. A person on that network can read the traffic, including the password at sign-in. Use it only on a trusted network, never forward its port from your router, and put an HTTPS reverse proxy in front for more protection (see [Hosting](docs/hosting.md#https-with-a-reverse-proxy)). Behind HTTPS, set `ART_FRAMES_SECURE_COOKIES=1` together with `ART_FRAMES_BIND=127.0.0.1`, so plain HTTP is not open next to the proxy. Art Frames logs a warning when the cookie is Secure and the bind is not loopback. Scripts can still send HTTP Basic auth; Art Frames never asks for it.
- Every POST needs a session CSRF token. The sign-in and setup forms carry their own form token, bound to the browser's session before sign-in, and also reject a request whose `Origin` names another site or whose `Sec-Fetch-Site` is `cross-site` or `same-site`. Pages send `Referrer-Policy: same-origin`. Cookies are HttpOnly and SameSite=Lax, with Secure available for HTTPS.
- Art Frames answers only to allowed host names. On loopback the list is explicit. On the home network the default `*` allows only a private or loopback IP address, a `.local` name or a name without dots; other names get "Bad Request". This blocks DNS rebinding from public names.
- The update check is the only request Art Frames itself makes to the internet. At most once a day it asks the GitHub API for the newest release of `brianmuse/art-frames`. It sends no ID, version or settings, and it never installs anything. Turn it off in **Server → Updates** or with `ART_FRAMES_UPDATE_CHECK=0`. It is off by default for an install with a password file.
- Templates escape artwork metadata. A restrictive Content Security Policy blocks third-party scripts, framing, plugins, and cross-origin form submissions. No CDN scripts or analytics are used.
- Public-source fetches require HTTPS, validate every redirect, reject private/link-local/loopback destinations, and pin the checked IP while verifying TLS against the original hostname. Museum image imports are restricted to approved hosts. Downloads and image dimensions are bounded.
- Artwork is decoded and re-encoded without embedded EXIF metadata. Files are addressed by generated UUIDs, never uploaded filenames.
- Config, pairing tokens, the password hash and the session key live in files that Art Frames owns in the data folder. Atomic writes create mode-0600 files. The CLI sets umask 077. Site secrets are masked in forms and never added to core snapshots. A settings backup holds TV tokens, sensor keys, layout secret values and TV MAC addresses only when you tick **Include private values**; without it, the file still holds TV names and IP addresses; it never holds the password or the session key. Keep such a file private.
- The container runs without root privileges, Linux capabilities, a writable root filesystem, or a Docker socket mount. Mount only Art Frames' own data and read-only config. Host networking permits TV control/Wake-on-LAN, so network isolation is intentionally weaker than Docker bridge mode; filesystem access is still constrained by mounts and the process identity.
- Cleanup deletes only content IDs uploaded and recorded by the current Art Frames instance. It does not enumerate and remove unrelated TV art.

## Operator responsibilities and limits

Keep the host operating system, Docker and Art Frames up to date. Use a unique strong password. Allow administration of the host only from your home network or a VPN. Pairing with a TV grants Art Frames control of its Art Mode; review the TV's allowed-device list when retiring an installation.

Art Frames does not encrypt its data at rest. Protect the data volume or folder with host permissions and, if you need it, disk encryption and backups. A host administrator, or an attacker who controls the host, can read the container data. An unprivileged container does not protect against a compromised host or every kernel or container-runtime vulnerability.

The mDNS announcement (`art-frames.local`) tells devices on the home network that an Art Frames server is there and on which port. Turn it off with `ART_FRAMES_MDNS=0`.

Image rights metadata describes the source at import time. The software does not grant additional rights to artwork. Public museum search queries and museum thumbnails go to their respective providers. Installed layouts define their own external integrations and must document data sent to their providers. There are no required AI APIs or uploads of user artwork to an AI service.

## Website rendering boundary

The website is a static build under the explicitly mounted `sites` folder. Paths, symlinks, hidden files, and synthetic origins are validated before assets are supplied. The browser runs nonroot with Chromium sandboxing and no network. It receives only bounded local assets and opted-in page data over a private Unix socket; it has no NAS/data/config/password/token mounts. Every render has an overall deadline, including font/readiness waits. The controller mounts the socket volume read-only. The optional host-network configuration applies only to the controller. In the source Compose files its HTTP bind remains loopback; the install file binds it to the home network.

Generic websites receive no artwork context by default. Do not put credentials in website source, public assets, or page data. The management UI shows only a rendered image and does not execute custom page scripts in its authenticated origin.

## HTML/CSS renderer

The explicit HTML/CSS deployment avoids executing browser JavaScript. It retains the networkless renderer, no app/credential mounts, read-only roots, dropped capabilities, no-new-privileges, memory limits, bounded resource requests, and a killable per-render process with resource limits. File and external URL fetches are blocked, including nested stylesheet/SVG resources. It is not a route to running Chromium without its sandbox.

Hosts without seccomp or PID cgroups still lack those kernel protections; process-level limits do not create equivalent isolation. Install only trusted website/server code. The generic starter service is separately networkless. Trusted installed layout servers can request bridge networking and receive their own read-only private settings directory. They never receive controller data, management credentials or TV tokens. The renderer stays networkless. Page text is escaped, and concurrent requests do not share snapshot state. See [renderer choices](docs/renderers.md) for supported content and guarantees.

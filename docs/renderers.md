# Renderer choices

Art Frames explicitly selects a renderer through the Compose file. It never silently switches from a failed browser to a less restricted browser.

| Mode | Compose files | Content | Host requirements |
| --- | --- | --- | --- |
| Chromium | `compose.yaml`, `compose.host.yaml` | HTML/CSS and browser JavaScript | seccomp, PID cgroups, enforced memory limits, and a working Chromium sandbox |
| HTML/CSS | `compose.static.yaml`, `compose.static.host.yaml` | Complete HTML and supported CSS, without browser JavaScript | enforced memory limits and ordinary Linux process resource limits |

The host variants give only the controller host networking for LAN wake broadcasts. Management HTTP stays on loopback in every variant. Both renderers keep network access disabled and receive site assets over their private socket. Neither gets NAS data, configuration, password, TV-token, or Docker-socket mounts.

## Lightweight HTML/CSS mode

WeasyPrint lays out one 3840×2160 page, and PDFium rasterizes it to JPEG. A render runs in a disposable child process with a 35-second supervisor deadline and CPU, address-space, file-size, and process resource limits. Memory is also limited by Docker. The worker root filesystem is read-only, its capabilities are dropped, and no-new-privileges remains enabled. File and external HTTP resource URLs are rejected; use ordinary relative files in the selected site.

This mode does not execute browser JavaScript. Hydrated components, canvas, WebGL, animated layouts, and browser-only API calls need the Chromium mode on a compatible host. WeasyPrint implements a [documented HTML/CSS subset](https://doc.courtbouillon.org/weasyprint/stable/api_reference.html#supported-features); websites should use a fixed screen layout. Multiple-page output fails instead of silently selecting part of a page. Browser readiness selectors do not run scripts in HTML/CSS mode.

On a host without seccomp or PID cgroups, this mode does **not** supply those kernel isolation features. It uses the host's ordinary container isolation plus the restrictions above and process-level resource limits. Use trusted installed website/server code. A compatible Linux host is required if your deployment needs kernel syscall filtering or cgroup process containment. Do not describe this mode as security-equivalent to the Chromium deployment.

## Dynamic Astro content without a browser

The lightweight Compose files run a separate website service. The generic starter is networkless. An installed layout can request bridge networking for its own APIs and receives only its own read-only settings directory plus its private socket volume. The controller supplies per-request context and optional selected-art bytes; no controller password, TV token or catalog is mounted into the layout server. Each request gets its own `Astro.locals.snapshot`. The renderer itself stays networkless and receives no configuration.

An installed layout renders its own content into HTML before image rendering. You can keep editing ordinary `.astro` components, styles, and server-side JavaScript. The content contract remains generic:

- A nonempty `site.path` selects your own read-only static build and bypasses the default website service.
- A blank `site.path` selects the default website. The optional `DAYFRAME_DEFAULT_SITE_SOCKET` deployment variable supplies a server-rendered default; without it, Art Frames uses the bundled static build.
- You can replace the default website service with another framework implementing the [website service contract](website.md#website-servers). No content-specific provider is required.

For development, `npm run dev` uses sample page data. `npm run build` produces the regular static build. `DAYFRAME_SITE_MODE=service npm run build` produces the Astro server build used by `Dockerfile.website`. The server service is optional for custom static builds; omit it from a custom Compose deployment when you do not use it.

## Validation

`sh scripts/check-host.sh MODE` reads Docker's capabilities before builds. `sh scripts/check-containers.sh APP_IMAGE RENDERER_IMAGE MODE WEBSITE_IMAGE` verifies the isolated pipeline. The HTML/CSS check covers server-rendered and escaped page data, separate concurrent snapshots, 4K starter/dashboard/art output, and blocked filesystem/external asset URLs. It uses synthetic data and does not contact a TV.

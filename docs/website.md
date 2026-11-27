# Website contract

Bring a self-contained static build, or a trusted website server. Astro is an example framework. Websites own API choices, calendar/weather integrations, content schedules and presentation. Art Frames owns capture and delivery.

## Static builds

Set `site.path` relative to the read-only sites mount and select an HTML entry. The renderer sees a synthetic `http://dayframe.local/` origin. Only bounded local assets are supplied; external and filesystem URLs are blocked, including nested CSS/SVG resources. Bundle fonts and dependencies. Bounded base64 JPEG/PNG/WebP data URIs are also supported by the HTML/CSS renderer.

The Chromium profile can execute browser JavaScript in its sandbox. The lightweight HTML/CSS renderer needs complete HTML and does not execute JavaScript. The management UI displays an image and never runs site scripts in its authenticated origin.

## Context

Every snapshot contains `version`, current `now`, `timezone`, display dimensions and the user’s generic `data` object. Optional `art` context provides the daily selected artwork metadata and `/dayframe-art.jpg`. Core snapshots contain no calendar/weather data or site settings. The same selected art survives morning/evening transitions; each layout decides how to present it.

An installed layout also gets `display_settings`: this TV's values for the per-TV settings the layout's `layout.json` declares, for example `{"show_caption": true}`. Art Frames sends only declared keys, fills in each default and drops unknown keys. Values are booleans. A custom website build without a manifest gets no `display_settings`. Changing a value re-renders only that TV. Art Frames stores the values under the layout's settings scope (`DAYFRAME_LAYOUT_CONFIG_ID`, or the layout ID), so renaming the layout ID keeps them. Saving the **On each TV** section of the layout's settings page keeps only the installed layout's values.

When a mat is enabled, `art.viewport` describes the actual painting opening as integer `x`, `y`, `width` and `height` values in the 3840×2160 output. Layouts should lay out their content inside this rectangle. Art Frames restores the surrounding mat from the selected artwork after capture, so website gradients and other pixels cannot paint over it. Without a mat, this field is absent and capture remains full-screen. A TV set to **Never show mats** gets the painting without its mat and no `art.viewport`.

A server-rendered website additionally receives `art.image_data` (base64 JPEG) when artwork is enabled, allowing layout-owned image processing without a NAS data mount. This field goes only to the selected server; the public `/dayframe.json` resource keeps the image URL. Do not serialize private server state or configuration into pages.

## Website servers

Both static Compose profiles enable `DAYFRAME_DEFAULT_SITE_SOCKET`. The controller POSTs a per-request context to the selected HTML path over `/run/dayframe-site/site.sock`; the server returns complete HTML. Later local asset GETs use the same socket. Requests are bounded to 40 MB of context, responses to 25 MB, and each website response to 20 seconds within the overall render deadline. Concurrent contexts must remain request-local.

The bundled generic server is networkless. Installed layout servers receive only their scoped read-only `/site-config` directory and their private socket volume. A manifest may request bridge networking for its own integrations. No TV token, management password, artwork catalog or unrelated NAS share is mounted into the server. The renderer never receives this configuration or network access.

## Development

`website/` is the generic starter: `npm --prefix website ci`, then `npm run dev`. The separate `art-frames-layouts/layouts/morning` project owns the actual morning screen, provider code and fixtures. Its README describes live local data and explicit sample mode. A different layout can use any stack that produces this HTML/asset contract.

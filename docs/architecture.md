# Architecture

Art Frames has a Python controller with a Flask management UI and one scheduler, plus a networkless browser worker. The compiled website is the content boundary. SQLite and images support optional gallery data. Astro runs at build time, and there is no required database server or cloud account.

| Module | Owns |
| --- | --- |
| `config.py` | Pydantic schema, TOML defaults, atomic JSON overrides, core settings |
| `library.py` | Catalog schema, image normalization, framing, selection flags, daily rotation history |
| `sources.py` | Museum-specific search, metadata, rights checks, approved image hosts |
| `network.py` | Bounded HTTPS, public-address validation, redirect revalidation, DNS-pinned TLS |
| `layouts.py` | Generic layout manifests and private per-layout settings |
| `site.py` | Build roots, local assets, optional snapshots, revisions, and capture |
| `ipc.py` | Size-bounded JSON frames over the private renderer socket |
| `renderer/server.py` | Networkless browser capture with a render deadline |
| `television.py` | Explicit pair, status, wake, image upload/selection, owned-image cleanup |
| `controller.py` | One content/schedule owner; wake events independent from desired content |
| `tv_recovery.py` | Read-only search for a paired TV after its DHCP address changes; applies only a neighbour-table-confirmed match |
| `activity.py` | Orders, filters and groups the job activity record for the Activity page |
| `web.py` | Authentication, CSRF, browser forms, page composition |
| `__main__.py` | Deployment environment, file umask, scheduler lifecycle, Waitress startup |

## Ownership boundaries

The separately installed website owns its content and API integrations. The website decides what to display. The scheduler controls refresh timing, and the TV adapter checks whether Art Mode is awake and safe to update. Wake is an explicit operation, never an automatic consequence of a connection retry. Preview does not send commands to a real TV.

Art Frames' own storage is the source of truth for original artwork, rendered images, rights metadata, selected pieces, and daily rotation history. TV storage is a display cache. Before selecting a newly uploaded image, the controller durably records that content ID as belonging to this instance. Cleanup operates only on those recorded IDs.

The web server and scheduler share an in-process lock for display operations. Scheduler ticks also take a file lock across processes. SQLite uses transactions, WAL, and a busy timeout. One process schedules up to eight TVs sequentially to bound NAS rendering memory. One process-wide render lock serializes website captures across all TVs. Each TV has a stable ID, independent delivery state, pairing-token directory and preview. The original TV retains its existing storage paths. Multiple controller processes sharing runtime data are not a supported deployment topology. Web buttons do not call the controller directly: `jobs.py` keeps an in-memory queue per TV with one daemon worker thread, which takes the same locks and waits up to 10 minutes for a scheduler pass. The scheduler still never waits and skips a busy pass. Address recovery (`tv_recovery.py`) runs a search on its own daemon thread, so a sweep never delays a tick. A module-wide lock allows one search at a time across all TVs. The next tick applies the result under the TV lock. The same module keeps the activity record that `/activity.json` serves.

## Multiple TVs and reusable sites

The header selects a stable TV ID carried explicitly in every management URL. Hardware actions and image requests resolve their target from that URL, so another tab cannot silently change the target. The original TV is `main`; additional TVs have generated IDs stored in validated installation settings. A duplicate name or LAN address is rejected.

The catalog, framing and selection flags are shared. Daily artwork assignments are separate per display and allocated in a SQLite write transaction. A display avoids artwork assigned to another TV that day whenever another selected piece is available, falling back gracefully for small collections. Existing `art_days` records remain the original TV's history.

Additional TVs inherit the shared website by default; they may override a static website build or refresh settings. Installed layout settings and secrets remain shared by all TVs using that layout. Each TV has its own connection and wake settings. Adding a TV never wakes it or pairs it automatically. A failed or unavailable TV does not prevent subsequent TVs from updating. Rendering is sequential in the scheduler, using the existing isolated renderer service.

One installation can run several installed layouts side by side. Each layout has its own container, socket and settings scope. Adding a TV does not duplicate a layout or its API caches: every TV that shows a layout uses the same container. Installing layouts is a deploy step: `npm run deploy` installs every layout in the layouts repo's catalog and in any extra `[[sources]]`. The management site only selects among the installed layouts, for each TV, and edits their settings. It never installs, fetches or removes layout code.

### Layout schedule and rotation

Each TV has a layout schedule (`apps.py`): layout blocks for each day. Outside the blocks the TV shows the built-in Artwork, which Art Frames composes itself (`artwork.py`). Where two or more blocks overlap, their layouts take turns. At each moment the schedule gives a plan: the layouts to show, the swap time and the next change.

The render pass and the swap timer are separate:

1. The render pass (every 30 seconds) renders a layout only when its revision changes. The revision changes on the render interval (`site.refresh_minutes`), or when the layout's settings, artwork or files change. In a rotation each layout has its own revision. A pass renders and uploads at most one layout, so one TV never holds the render lock for long.
2. Art Frames stores the content ID of each layout's last upload (`app_images:<device>`). The TV keeps at most one image for each layout in use. Cleanup deletes the images of layouts the TV no longer shows.
3. The swap timer (its own thread, every 5 seconds) shows the next layout of a due rotation with one select call. It never renders or uploads.
4. A swap runs only when the last status reading says the TV is paired, on, in Art Mode and not playing a programme. The TV then confirms power and Art Mode again before the select. In standby, while watching or in an unknown state, the swap is skipped without a retry and without a wake.
5. A swap takes the TV lock without waiting. If an upload or any other TV operation runs, the swap skips that turn. So swaps and uploads to one TV never overlap.
6. At a slot change, a single layout whose stored image is still current is selected again without a render.
7. The wake handoff renders the layout that the schedule shows at wake time. The render starts up to 10 minutes before the wake and uses the wake time, so its revision is the revision at the wake. A delivery in the wake window uses that image, also when it runs in the next refresh period.
8. From 2 minutes to 1 minute before a wake, a TV in confirmed standby gets the wake-time image of a single layout (not a rotation). The TV reads its power state before the upload and again before the selection. It selects the image with `show=False`, so the screen stays off. At the wake, the TV only selects that image. Apart from the owner's standby image test, this is the only time a TV in standby gets artwork. It never sends a key, a wake packet or a brightness change, and it starts at most twice per wake (`prewake_day:<device>`). A delivery in the wake window accepts the wake-time key only in the first refresh period after the wake.

Activity lists renders and uploads. Swaps are routine, so Art Frames counts them per day for the Overview and does not list them.

## Health and analytics

Each TV has a **Health** page (`/tv/<id>/health`). It answers "Is this TV OK, and what do I fix?" from stored state only. It never contacts the TV. From top to bottom, it shows:

1. The checks, in order: the scheduler runs, the TV is paired, the TV answered at the last check, the last scheduled update, each layout of the TV's schedule, and the last wake. Each problem has one fix button.
2. Suggested fixes from the last 14 days.
3. Recent problems.
4. Charts of 7, 30 or 90 days in the TV's timezone.

The sidebar shows the number of problems next to **Health**. The old `/tv/<id>/insights` and `/analytics?tv=<id>` URLs redirect there.

The scheduler does not stay stuck. Each TV connection has a total time limit: on the art channel, 20 s for checks, 120 s for Art Mode settings and 300 s for an upload, a soft blank or its undo; on the remote-control channel (keys and pairing), the reply timeout plus 15 s. Renders have their own limits. A watchdog thread checks the scheduler every minute. When one step of a pass (one TV's wake check, update or sample) runs for more than 45 minutes, it logs where each thread is, saves queued analytics rows and ends the controller with exit code 70. Docker's `restart: unless-stopped` policy then starts it again. Web jobs that were still waiting in the in-memory queue are lost; start them again.

Art Frames collects three kinds of data, in `analytics.sqlite` in each TV's state folder:

1. **Samples.** Every 15 minutes, the scheduler takes one read-only sample per TV, in any state. A sample stores the time, whether the TV answered and how long it took, power, Art Mode, and whether a programme is playing. In Art Mode, it also stores brightness, the light sensor, color temperature and the selected content ID. A sample never wakes the TV, changes its mode or interrupts viewing. It does not wait for the TV lock: if another operation holds the lock, the next scheduler pass takes the sample.
2. **State changes.** The scheduler checks the TV about every 30 seconds and records each change (Art Mode, watching, standby, unknown). On-time comes from these changes, so it is exact. After a restart, the time the host was down counts as unknown.
3. **Reliability events.** Scheduled wake results, image deliveries with their duration, and failed stages.

Art Frames never stores the TV's IP address, MAC address, network name, unique IDs or name in this file. One background thread does all writes, so web pages and the scheduler never wait for the database. The file uses WAL mode and keeps one year; Art Frames removes older rows once a day. The charts are inline SVG drawn on the server, so the page needs no script.

## Provider extension

A museum adapter must return the same artwork metadata shape, expose a stable provider/object ID, check the provider's image rights at import time, and use the public HTTPS fetcher with an explicit host allowlist. Imports accept a provider ID and object ID, never an arbitrary browser-supplied image URL. Add the provider name to the typed config schema and UI labels, then add adapter contract tests.

The Met adapter uses its paginated v1.1 search endpoint, with the v1 object-detail endpoint. The Chicago adapter uses public-domain-filtered search, detail metadata, and its documented IIIF image service. Browse requests fetch bounded metadata; only selected imports download full images.

## Schema evolution

The initial catalog schema is version 1. An unexpected schema version fails startup. Future releases must add explicit, transactional migrations and a pre-migration backup step. Never silently recreate an existing catalog or drop runtime data. Software upgrades and artwork-library operations are separate responsibilities.

## Website isolation

The controller supplies only the selected build root and opted-in snapshot resources over a named-volume Unix socket. The browser has no network, website/data/config/credential mounts, or Docker socket. The controller sees its socket volume read-only. Pairing tokens and uploaded-image ownership are scoped to each configured device identity; cleanup retains IDs until the TV acknowledges deletion. Static-site previews are images, so page scripts never run in the authenticated management origin.

## Layout repositories

`art-frames-layouts/catalog.json` declares the default and premade layout paths. Each package owns a manifest, source, Dockerfile, providers and tests. Deploy installs every catalog layout from a pinned Git archive or committed local HEAD, and records each layout's source in `.dayframe-source.json`. The management site reads what is installed; it does not move domain settings into Art Frames. Secret storage is generic and scoped to the selected layout. See [layout packages](layouts.md).

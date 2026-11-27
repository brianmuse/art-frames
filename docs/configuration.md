# Configuration

This page covers source installs that use `config.local.toml`. With the install file (`install/docker-compose.yml`), set everything in the web app; for its `environment:` settings, see [Hosting](hosting.md#settings).

Copy `config.example.toml` to ignored `config.local.toml`. Runtime browser saves are complete validated snapshots in private `data/settings.json` and take precedence over TOML. Do not edit that JSON while Art Frames is running. New settings take effect on the next scheduler tick; previews and manual refresh read current settings.

| Section | Responsibility |
| --- | --- |
| `timezone` | IANA timezone for the host clock and wake schedule |
| `site` | Static build folder/entry, refresh minutes, readiness selector, generic JSON data and optional `art` context |
| `wake` | The bounded grace window of a scheduled wake. The wake times are in `app_schedule` |
| `art` | Library enabled state, shuffle/sequential rotation, daily boundary and museum sources |
| `tv` | Private IP, MAC, API port, timeout and wake broadcast address |
| `app_schedule` | Per TV: a day timeline of layout blocks and a turn-on time, with optional days of their own. Artwork shows where no block is |

### Layout schedule

Set the layout schedule on each TV's Schedule page, on its timeline. Art Frames saves it with that TV's settings:

```json
"app_schedule": {
  "default": {"apps": ["morning"]},
  "week": {
    "default": {"blocks": [{"app": "news", "start": "07:00", "end": "09:00"},
                           {"app": "photos", "start": "08:00", "end": "10:00"}],
                "wake": "06:45"},
    "days": {"mon": null, "tue": null, "wed": null, "thu": null, "fri": null,
             "sat": {"blocks": [], "wake": null}, "sun": null},
    "swap_seconds": 300
  },
  "turns_note": false
}
```

- Where no block is, the TV shows the day's artwork. Art Frames composes it from the Artwork page's framing; it needs no layout. Older saves may hold a `default` layout for the rest of the time. Art Frames keeps it so they load, but ignores it.
- `week.default` is every day's plan. A day in `week.days` with its own plan uses it instead. `null` means the same as every day.
- A block shows one installed layout from `start` to `end` ("HH:MM"). Blocks never cross midnight: `end` is "24:00" at the latest. The Schedule page adds at most 24 blocks to a day. A saved day can hold up to 144, so older time rules always load.
- Where two or more layouts share a time, they take turns every `week.swap_seconds`: 30 to 3600 seconds, 300 by default. This is one speed for the whole TV. A swap shows the layout's last render.
- `wake` is the day's turn-on time, or `null`. A turn-on time needs the TV's IP and MAC address.
- The layout ID `artwork` is reserved for the built-in Artwork. The installer refuses a layout with that ID.
- Older saves with `windows` (time rules) and a `wake` clock with `enabled`, `at` and `days` load in this shape. Each rule and wake keeps its days and times.
  - The most common day becomes every day's plan. On a tie, the earlier weekday wins. Each other day gets its own plan.
  - The turn speed is the most common one of the older rules that took turns. When they had different speeds, `turns_note` is `true`, and the Schedule page says so once.
  - Before the first save in this shape, Art Frames keeps a copy of the older file as `settings.pre-week.json`. See [Rollback and settings](cli-deployment.md#rollback-and-settings).
- "Render every, minutes" is `site.refresh_minutes`. Each layout in use renders this often.
- A layout that is no longer installed is replaced by the first layout until you change the schedule.

Art Frames has no calendar, weather, holiday or planner-window schema. Installed layouts own those features and may declare their settings through a manifest. See [layout packages](layouts.md).

## TV and time semantics

Reserve each TV’s IP address in your router (a DHCP reservation). Art Frames reaches each TV at its saved address. Art Frames identifies a TV by its MAC address, so pairing and Art Frames' uploads carry over when you change the TV’s address, MAC or port.

### Address recovery

If the router gives a paired TV a new address, Art Frames can find it again. Recovery needs the TV’s MAC address in Settings. The search is read-only: it does not pair, wake or control any device.

A search starts only when one of these happens:

1. Art Frames sent a wake and the TV did not answer at its saved address.
2. The TV status was unknown for 24 hours. This search does not start in the 5 minutes before a scheduled wake.
3. You select **Find this TV** in Settings.

Searches 1 and 2 run at most every 30 minutes for each TV. If search 2 keeps finding no TV, Art Frames doubles the wait after each search, up to 6 hours. This happens, for example, on a weekend with weekday-only wakes, or when the TV is unplugged. The wait goes back to 30 minutes when the TV answers. Only one search runs at a time across all TVs. A TV in normal nightly standby does not start a search.

A search first checks the kernel neighbour table (`/proc/net/arp`), then sweeps the TV’s /24 network and the host’s own /24 network with the same limits as Find TVs. Art Frames changes the address automatically only when:

- exactly one device answers with the TV’s MAC address, and
- the neighbour table, read again after the sweep, maps that address to the same MAC.

Any device can claim a MAC address in its device-info reply, so a reply alone is not trusted. If the neighbour table cannot confirm the match, Settings shows: “Art Frames found a TV that reports this TV’s MAC address at <address>. Use this address?” Select **Use this address** to apply it, or **Keep the saved address**. A device whose neighbour-table entry shows a different MAC is ignored.

Settings shows the time and result of the last search. When the TV answers at its saved address, Art Frames clears the result and any proposal. It keeps two results: “Found this TV at a new address and updated it” and “The TV answered at its saved address”. Art Frames also clears a proposal when you save settings, and a proposal expires after 1 hour. Before **Use this address** applies a proposal, Art Frames checks the address again: the device there must still report the TV’s MAC address.

Limits:

- On macOS and in Docker bridge networking, the neighbour table is missing or lists only the gateway. Art Frames then never changes the address automatically; it shows a proposal instead.
- A wired TV does not report its MAC address. Art Frames finds it only through the neighbour table, so in bridge networking it cannot find a wired TV.

A DHCP reservation is still the recommended setup.

Find TVs searches a private IPv4 network through Samsung’s port-8001 device-info endpoint. It uses at most 256 addresses, twelve simultaneous probes, one second per probe and a 30-second search interval. Authentication and CSRF protection apply. Search does not pair, wake, save or control a TV. Names can be stale; verify the target and pair separately while awake.

Wake is off by default. It is recorded before sending packets, does not catch up outside its grace window and never repeats after later inactivity sleep. If the TV does not confirm Art Mode, Art Frames sends Wake-on-LAN again once per scheduler pass until the grace window ends. It stops as soon as the TV reports Art Mode or a programme. During that window, a TV that is back in confirmed standby is switched to Art Mode again; this is by design. Active programmes take priority. Routine refresh does not wake a sleeping screen. Validate these behaviours on the real model.

From 2 minutes to 1 minute before the wake, a TV that answers in standby gets the wake-time image early. Art Frames uploads it and makes it the TV's current artwork without showing it, so the screen stays off. At the wake, the TV only switches to that image, so last night's image does not stay on screen while a new one uploads. Art Frames tries this at most twice per wake. It sends no key, wake packet or brightness change. A TV that is on, plays a programme or does not answer gets nothing early: it gets the image after the wake, as before. A rotation at the wake also gets its images after the wake. The log shows a line for each early upload. When a check in that time finds the TV not ready, the log says why. There is no line when no scheduler pass falls in that time, when the render fails or waits after a failure, or while a programme plays.

### Room sensor: when the room is empty

Set these on each TV's Sensor page, in **When the room is empty**. They count from when the sensor last saw someone. A gap of up to 5 min with no presence reading (the sensor link drops and comes back, or the sensor sends no value) does not start the count again; a longer gap does. No step starts during a gap: the next "empty" reading runs any step that is due. While the room is empty and the dim waits, the brightness can go down with the room light, but never up. The steps run in order:

1. **Dim after** (`sensor.away_after`, default 0 = at once): the lowest brightness of the curve.
2. **Blank after** (`sensor.blank_after`, default 15 min): a black image at the TV's lowest brightness. A Frame at its lowest brightness still glows a little.
3. **Turn the TV off** (`sensor.off_after`, default 30 min): Art Frames sets the TV's own **Sleep After** timer to its shortest value, 5 min. The TV then turns itself off. Pick **Never** to keep the TV on. In `settings.json` that is `null`; in a TOML file, write `off_after = "never"`. The Sensor page offers 15 min, 30 min, 1 hour, 2 hours and 4 hours. Pick a time later than the blank time; the page refuses a save that does not fit. Settings saved before this setting existed get the first of these times that is later than their blank time and at least 30 min: a blank of 5 to 15 min turns off after 30 min, a blank of 30 min after 1 hour, and a blank of 4 hours never. An older open page that has no off field keeps the saved off time while it is still later than the new blank time.

Art Frames sends no power key to turn the TV off. On a Frame in Art Mode, the power-off key does nothing. The TV's own Sleep After timer does work:

- The TV goes off a few minutes after the off time, by the shortest Sleep After value (5 min). With an off time of 30 min, the TV is off about 35 min after the room is empty.
- This works only when the TV's own motion sensor sees no one. A frame that covers the sensor does that. If the TV's sensor sees motion, the TV stays on.
- The blank stays on the screen until the TV turns itself off.
- A gap in the sensor readings, or a restart of Art Frames, keeps this Sleep After. Someone in the room, a TV that is on again, or new settings turn it off.
- Art Frames saves your own Sleep After value first. Turning sensor control off puts it back.

Art Frames sets Sleep After only when the screen is blank, the room has been empty for the off time, the sensor reports the room, and the TV is on in Art Mode. It does not set it while someone watches TV, and not from 10 minutes before a scheduled wake to the end of its grace window. If the off step already set Sleep After when a scheduled wake comes near, Art Frames turns Sleep After off again, so the TV does not turn itself off while the wake turns it on.

After the set hours, Art Frames sets the shortest Sleep After when the room has been empty, or without a sensor reading, for 2 min. A sensor that flickers between empty and someone there does not change Sleep After back and forth. When someone comes in, Art Frames turns Sleep After off at once.

When someone comes in during the set hours, Art Frames turns Sleep After off. This also works when someone comes in just after the TV turned itself off, before Art Frames has read the TV's new state. If the TV is in standby, Art Frames turns it on again (one power key from standby, or the wake signal). The first try after the TV turned itself off never waits for the 10-minute gap between screen-on tries. When the TV is on in Art Mode again, the saved image and the brightness for the room light come back, and the empty room counts from then. This also applies when the TV comes on another way, such as the remote or **Turn on now**: the TV does not turn off again at once. Outside the set hours, someone walking in does not turn the TV on.

A TV that does not answer at all may be fully off. When it is on in Art Mode again, the off step waits its full time.

The log has one line when Art Frames sets Sleep After, such as `TV <id>: room empty for 30 min; Sleep After 5 min so the TV turns itself off`, and one line when the TV is in standby after it. When the TV does not take the Sleep After change, the log has one line for each new reason, such as `TV <id>: Sleep After not changed (rejected); Art Frames tries again in 5 min`. The log also says when the sensor link drops and when it comes back: `Sensor <name>: connection lost; reconnecting` and `Sensor <name>: connection back (api)`. It has at most one drop line in 10 min; the next drop line gives the number of drops in between. A gap of 1 min or more without a presence reading gets one line when the readings come back, and a gap longer than 5 min gets one line when the empty-room count starts again.

If someone comes in after the TV turned itself off, and the TV does not turn on, the Now page says "Someone is here, but the TV did not turn on. See Health." The Health page says why and when Art Frames tries again.

### Room sensor: when the room is dark

**Blank when the room is darker than** (`sensor.dark_below`, in lux, default **5 lux**) keeps the artwork off in a dark room. It is on for every TV with sensor control. Sensor control turns off the TV's own light sensor, so Samsung's Night Mode cannot blank a dark room. Use this setting to do it instead. The Sensor page offers 1, 2, 5, 10, 20 and 50 lux. To turn it off, pick **Never**; in a settings file, write `"never"`.

1. When the room light (the median of the last 2 min) is below this level, Art Frames dims and then blanks the screen at once. It does this even with someone in the room, and during a brightness pause. With **Turn the TV off** set, it also sets the shortest Sleep After at once, so the TV turns itself off about 5 min later.
2. While the room is dark, nothing turns the screen on: someone walking in does not, and the scheduled wake waits. The Health page says "Last turn-on was skipped because the room was dark". Each pass in the wake's grace window checks again, so the wake runs once the light is back. The image before the wake is not uploaded while the room is dark.
3. The room counts as light again only at twice the level. When the light is back and someone is there, the artwork and the brightness come back, and a TV in standby turns on during the set hours. When the light is back and the room is empty, the screen stays blank.
4. Without a fresh light reading, a blank already on the screen stays. A gap of less than 5 minutes keeps the room dark, so the scheduled wake still waits. After a longer gap, the scheduled wake counts the room as light, so a broken sensor never keeps the TV off for good.
5. **Undo blank** shows the artwork, but in a dark room sensor control blanks the screen again.

The artwork's own glow reaches the sensor. Pick a level above what the sensor reads with the lights off and the artwork on, or the room never counts as dark. The Sensor page shows the room light now next to the setting.

The log has one line when the room becomes dark, such as `TV <id>: room dark (0.4 lux, below 2); the screen blanks and stays off until the light is back`, and one when the light is back, such as `TV <id>: room light again (35 lux)`.

The daily art key follows the configured local boundary and survives restarts. A selected piece remains stable until the next art day unless excluded. Cleanup affects only content IDs recorded as uploaded by this Art Frames instance.

## Museum collections

Cleveland, the Met and Chicago are available; existing saved source choices are preserved. Enable a newly added collection in Settings. Artist searches and imports recheck public-domain rights and approved image hosts. Chicago can block API clients; that is reported separately from an empty page. Met records without downloadable open-access images are skipped with pagination retained. Users can also upload their own files.

## Runtime environment

Art Frames reads each setting as `ART_FRAMES_<NAME>` first, then as `DAYFRAME_<NAME>`, for example `ART_FRAMES_PORT`, then `DAYFRAME_PORT`. The install file uses the `ART_FRAMES_` names (see [Hosting](hosting.md#settings)). The source Compose files and `.env` use the `DAYFRAME_` names below. Keep them there: a rollback starts older images, and they read only `DAYFRAME_*`.

`DAYFRAME_DATA_DIR`, `DAYFRAME_CONFIG`, `DAYFRAME_PASSWORD_FILE`, `DAYFRAME_BIND`, `DAYFRAME_PORT`, `DAYFRAME_HOSTS`, `DAYFRAME_SECURE_COOKIES` and `DAYFRAME_LOG_LEVEL` configure the controller. `DAYFRAME_LOG_LEVEL` is `DEBUG`, `INFO` (default), `WARNING` or `ERROR`; set it in the installation’s `.env`. Logs never include TV tokens, calendar URLs or page content, at any level. Production credentials must travel over SSH or HTTPS.

`DAYFRAME_SITES_DIR` scopes custom builds. `DAYFRAME_DEFAULT_SITE_SOCKET` supplies an optional HTTP website server over a Unix socket. The installer sets `DAYFRAME_LAYOUT_ID`, `DAYFRAME_LAYOUT_CONFIG_ID`, `DAYFRAME_LAYOUT_ARTWORK`, `DAYFRAME_LAYOUT_MANIFEST`, `DAYFRAME_LAYOUT_ENTRY`, `DAYFRAME_SITE_BUILD_CONTEXT`, `DAYFRAME_SITE_DOCKERFILE`, `DAYFRAME_SITE_NETWORK` and `DAYFRAME_SITE_MEMORY` for a selected layout. These are host/package metadata; private layout settings stay in its scoped config file. `DAYFRAME_APPS` lists the other installed layouts as `<id>=<settings scope>` pairs; their manifests are at `$DAYFRAME_SITES_DIR/apps/<id>/layout.json`, and `DAYFRAME_APPS_SOCKET_DIR` (default `/run/dayframe-apps`) holds their sockets at `<id>/site.sock`.

An installed manifest owns its entry page; the corresponding core form fields are read-only. Static custom builds without a manifest remain editable. Removed dashboard fields are archived privately during upgrade, and deleted bundled dashboard entries migrate to the starter entry while custom site paths stay unchanged.

Artwork framing is stored per piece in the collection. The 100–150% Zoom slider trims edges in either presentation mode, with focal-point sliders positioning the enlarged image. Preview and saved 4K output use the same framing; originals remain unchanged. Existing catalog entries start at 100% zoom.

## Upgrade from the built-in dashboard

An upgrade from the former built-in dashboard keeps the old calendar and weather settings privately in `data/legacy-dashboard.json`. They are no longer in the core schema, and Art Frames never exports them to other layouts. Set up the new layout through its own declared fields. The kept values stay available for an explicit migration. Your artwork, TV tokens and daily selection history stay.

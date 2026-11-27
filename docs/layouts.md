# Layout packages and secrets

Art Frames is the host. A separately versioned repository can contain a catalog of layouts, or a single independent layout. The installer accepts either. The first catalog lives in `brianmuse/art-frames-layouts`.

```text
art-frames-layouts/
  catalog.json                 # schema version, default ID, ID/name/path entries
  layouts/
    morning/
      layout.json              # identity, entry page, runtime needs, settings schema
      Dockerfile
      package.json
      src/                     # Astro pages and layout-owned APIs
      server/                  # bounded calendar parsing worker
      tests/
```

`catalog.json` lists the layouts that the installer installs: all of them, with the `default` as the first layout. Installing is a deploy step. The management site only chooses among installed layouts; it does not install or remove them. There is no marketplace or community submission flow. A custom layout needs only a `layout.json` and Dockerfile and can be installed alone using `--app`.

## Layouts in the install file

The install file (`install/docker-compose.yml`) needs no installer. It runs three published layout images from the layouts repository: `ghcr.io/brianmuse/art-frames-layout-morning`, `-news` and `-weather`. The layouts repository's own release workflow builds them.

1. Each layout runs as its own `layout-<id>` service, with the same hardening as the other services and a bridge network for its own data.
2. At start, each layout image copies its `layout.json` into its socket folder. Art Frames reads it there: the first layout (`morning`) through `ART_FRAMES_LAYOUT_MANIFEST`, the others through `ART_FRAMES_APPS` and `ART_FRAMES_SITES_DIR`. So the settings page always matches the running layout version.
3. The settings scope of each layout is its ID. Art Frames writes the settings into a `layout-<id>-settings` volume, which the layout mounts read-only at `/site-config`.
4. To add another layout, such as Photos or your own, use a source install with the installer (below).

## Catalog format

`catalog.json` sits at the top of the repository or source folder:

```json
{
  "schema_version": 1,
  "default": "morning",
  "layouts": [
    {"id": "morning", "name": "Morning dashboard", "path": "layouts/morning"},
    {"id": "news", "name": "News & weather", "path": "layouts/news"}
  ]
}
```

1. `schema_version` must be `1`.
2. `layouts` lists at least one layout. Each entry has an `id`, a `path` and an optional `name`.
3. An `id` starts with a lowercase letter and holds only lowercase letters, digits and hyphens, up to 64 characters. It must match the `id` in that layout's `layout.json`. Each `id` appears once.
4. A `path` is relative and stays inside the source. It must not pass through a symbolic link. The folder holds `layout.json` and a Dockerfile.
5. `default` is optional and must name an entry. It applies only to the layouts repo (`[app]` in the deploy settings), where it picks the first layout. Without it, the first entry is the first layout. In a `[[sources]]` catalog, `default` has no effect: every layout there is an extra layout.

### Check a catalog

Source authors can check a catalog without installing anything:

```sh
.venv/bin/python scripts/install-layout.py --local-repo ../my-layouts --check
```

To check one source folder with the `[[sources]]` rules, use `--path` instead of `--local-repo`. The folder can be a plain folder or a folder in a Git repository:

```sh
.venv/bin/python scripts/install-layout.py --check --path ../my-layouts
```

The check reads the committed catalog, each `layout.json` and each package, the same way as an install. It prints `All <n> layouts are valid. Nothing was installed.` or the first problem it finds. When a `layout.json` uses a field that this Art Frames does not know, the message says to update Art Frames first, or to skip that layout.

In a Git repository, a file with a Git filter attribute, such as Git LFS, can't be checked. Art Frames names the file and stops. Remove the filter attribute, or skip that source.

## Manifest version 1

`layout.json` declares `schema_version: 1`, `id`, `name`, optional description, a relative HTML `entry`, `network` (`none` or `bridge`), memory in MB, whether it needs artwork, and a list of settings. Keys and identifiers are constrained; unknown fields fail validation. Field types are `text`, `secret`, `choice`, `time`, `checkbox`, `color`, and `list`. Colors use six-digit hexadecimal values. Lists declare scalar `fields`, an `item_label`, and `max_items` (up to 12); the editor provides add/remove controls and preserves each row’s private values independently. Choice defaults must be listed and time defaults use HH:MM. Secrets cannot have defaults. An optional `display_settings` list declares settings that each TV keeps for itself. Each item has a `key`, `type` (`boolean` only), `label`, optional `description`, a boolean `default` and an optional `badge` of up to 30 characters. The home screen shows the badge when a TV's value differs from the default. Keys must be unique and must differ from `settings` keys. The layout's settings page shows these items in an **On each TV** section, with one row per TV that runs the layout, and the snapshot sends them as `display_settings`.

Installing a layout applies its declared artwork capability consistently on local and remote hosts; replacing it with a layout that does not request artwork revokes that context. Turning off daily artwork also stops supplying an image. Art Frames does not interpret field names. A weather layout may declare a city and calendar credentials; a different site can declare unrelated fields or none. API endpoints, validation of provider-specific inputs, fetching, caching and error states belong to the layout.

## Secret handling

`/layouts` lists every installed layout. Each row shows whether the layout needs setup (a private setting is empty) or could not refresh on a TV, the TVs whose schedule shows it, and where it came from (`.dayframe-source.json`, see [CLI deployment](cli-deployment.md)).

Each layout's authenticated page, `/layouts/<id>`, is generated from the selected layout’s manifest. The first sentence of a setting's `description` shows under the field; the rest shows under "Learn more". Choice values show with a capital first letter. The page also lists the TVs that use the layout, its options on every TV (also a TV that does not show it yet), and a preview on the current TV. Password inputs never echo saved values. Empty input keeps the existing secret; an explicit clear checkbox removes it. A manifest fingerprint prevents an old form from writing into a replacement layout. Saving is atomic and CSRF-protected. List rows have stable IDs; removing a row removes its saved private fields. A list can declare `legacy_items` with non-secret `values` and `copy_from` mappings into secret fields, allowing a layout to move old scalar credentials into a list without exposing them. Migration happens on the next save and never overwrites an existing list, including an empty one. Deploy the supporting Art Frames host before installing a layout that uses a new field type.

Configuration lives in `data/layout-settings/<source-and-layout-scope>/config.json`: directory mode 0700, file mode 0600. The settings scope includes repository identity and layout path, remains stable across commit updates, and changes when the source changes. An unrelated layout reusing an ID cannot inherit the previous source’s credentials. One exception: a GitHub repository that Art Frames knows was renamed (`RENAMED_REPOSITORIES` in `scripts/install-layout.py`, for example `dayframe-layouts` to `art-frames-layouts`). When a layout from the new name has no saved settings, the installer copies them, and the layout's state, from its scope under the earlier name. `scripts/keep-layout-settings.sh` does the copy, locally and on the NAS during each update. It never replaces saved settings and keeps the earlier folders. At startup, Art Frames also copies each TV's options for the layout from the earlier scope when the TV has none under the new one. Only that active directory is mounted read-only at `/site-config` in the layout server. The layout reads `/site-config/config.json` server-side. Configuration never enters page snapshots, public static files, image builds or the renderer. The generic host still requires filesystem protection and backups: this is not encryption against a NAS administrator.

The layout server is trusted code and can use credentials granted to it. Install only reviewed sources. Bridge networking is an explicit server capability; the renderer itself remains networkless, with no NAS data or credential mounts.

## Installation and updates

Stop a local installation before replacing its source/configuration. The installer downloads a Git archive at a resolved commit or exports a local repository’s committed HEAD. It rejects archive traversal, symlinks/special files and committed private configuration directories. It replaces only an already marked `sites/installed` tree. Ignored files and Git credentials are not included.

The installer never runs Git inside a local repository. It reads a bare copy of it, so Git settings in that repository (such as `core.fsmonitor` or a filter) never run.

Use `--remote` with an existing SSH control connection for managed hosts. It sends the validated layout package separately because normal core sync intentionally excludes `sites`, `.env` and runtime data. Preparation validates a narrow runtime profile, preserves the rest of `.env`, assigns the selected private directory to runtime UID 10001, and retains the existing managed runtime symlinks. Then `remote.py up` builds and checks the new images before replacing running containers. Avoid simultaneous source syncs, layout installs or settings edits during deployment.

### Several installed layouts

One installation runs up to eight installed layouts. The installer installs every layout in the catalog. The catalog default is the first layout; give a catalog ID to choose another one, and `--skip` to leave layouts out:

```sh
.venv/bin/python scripts/install-layout.py --local-repo ../art-frames-layouts --skip news
```

Deploy settings can also add layouts from other catalogs with `[[sources]]` (see [deployment](cli-deployment.md#your-own-layouts)).

1. The first layout installs to `sites/installed`, runs as the `website` service and uses the `DAYFRAME_LAYOUT_*` keys in `.env`, as a single layout does.
2. Each other layout installs to `sites/apps/<id>` and runs as its own `app-<id>` service, built from its own Dockerfile.
3. Each layout has its own container, its own socket volume and its own settings scope. Only that scope's directory is mounted at `/site-config`, and only its state directory at `/site-state`. One layout never sees another layout's settings.
4. The layout still listens on `/run/dayframe-site/site.sock` inside its container. Art Frames reaches an extra layout's socket at `/run/dayframe-apps/<id>/site.sock`.
5. Each layout folder holds `.dayframe-source.json`: the source label, kind (`repo`, `path` or `git`), ref, commit, catalog name, layout path and install time, plus a GitHub link for a GitHub source. See [where each layout came from](cli-deployment.md#where-each-layout-came-from).

The installer and `prepare-remote.sh` write the extra services to `sites/compose.apps.yaml` with `scripts/app-compose.sh`. That script accepts only the validated layout profiles, so the file always has the `website` hardening. They also save the extra layouts as `DAYFRAME_APPS=<id>=<scope> ...` in `.env`. A layout that leaves the catalog, or that you skip, is removed on the next install, but its saved settings stay. Settings scopes follow the same rule as for one layout: a layout keeps its scope whether it is the first layout or an extra layout, and adding layouts never changes another layout's scope or saved values. Layout IDs must be different from each other, also across sources.

Each installed layout has its own settings page; the sidebar lists them all under Layouts. Each TV chooses when its layouts show on its Schedule page: layout blocks for each day. Overlapping blocks take turns. Outside the blocks the TV shows artwork. The per-TV settings of a layout (`display_settings`) show for each TV whose schedule uses that layout. See [configuration](configuration.md#layout-schedule).

Both static Compose profiles enable the website service and the extra layout services. Browser-only profiles serve static files directly; they do not start a layout server. With a browser profile, deploy installs no layouts and removes the layout keys (`DAYFRAME_LAYOUT_*`, `DAYFRAME_SITE_*` and `DAYFRAME_APPS`) from `.env`. For a local browser run, install only the first layout with `--first-only`. Existing saved artwork, tokens, database and core settings stay outside installed source.

Removed dashboard configuration is preserved in private `data/legacy-dashboard.json`. A layout does not receive this file automatically: importing previous credentials into a chosen layout must be an explicit operation. One installation serves its installed layouts to up to eight TVs. Each layout runs once, in its own container, however many TVs use it. Each TV follows the shared site or uses its own build.

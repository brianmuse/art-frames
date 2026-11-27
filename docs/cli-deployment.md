# Deployment from your computer

Use the shared `scripts/remote.py` helper with a **gitignored** `.deploy.local.toml` file. Only host metadata goes in that file. No NAS password, SSH private key, or GitHub token is stored by the helper. It requires Python 3.12+, Git, OpenSSH, and rsync locally, and a Linux host with SSH, rsync, OpenSSL, sudo, and Docker Compose. Synology's Container Manager supplies Docker; leave its required `docker` share in place.

Copy `deploy.example.toml` to `.deploy.local.toml` and edit the host, user, existing share path, and Compose variant. Use the exact case of the shared-folder name. The target is a dedicated new `dayframe` directory inside that share. Existing populated File Station installations are refused; migrate those deliberately instead of pointing this helper at them.

On Synology set `rsync_path = "/usr/bin/rsync"` to call the executable explicitly over SSH. No rsync daemon or additional network port needs to be enabled for this workflow.

For normal updates, run one command from the Art Frames checkout:

```sh
npm run deploy
```

This uses `.venv/bin/python scripts/remote.py update`; with another Python environment, invoke `python3.12 scripts/remote.py update` directly. It reconnects SSH when necessary, syncs committed core and optional layout source, prepares the runtime, builds and checks the images, activates the containers, verifies management health, then opens or refreshes the local tunnel. Existing art, calendar links, passwords and runtime settings are preserved. A failed build or capture check stops before container activation. Before it builds, the update tags the running images `:previous`. If the new containers do not become healthy, it restarts the `:previous` images and says so. After a healthy update it removes the images that were `:previous` before it, if no other tag uses them. Rollback restores the images and the Art Frames settings from before the update, not source or runtime preparation; keep the settings page closed during an update because its manifest may change before activation.

To see why something failed, run **`npm run logs`** (`remote.py logs`). It shows the last 200 lines from all containers. Name a service and add `--follow` to keep streaming, for example `npm run logs -- renderer --follow`. Add `--since 24h` (or `90m`) to show every line from that long ago in place of the last 200, for example `npm run logs -- dayframe --since 24h`. Services are `dayframe`, `renderer` and `website` (the first layout). Each extra layout is `app-<id>`, for example `npm run logs -- app-news`. If you name the first layout as `app-<id>`, the command says so and shows the `website` logs.

To go back to the images that ran before the last update, run **`npm run rollback`** (`remote.py rollback`). It restarts them with your configured Compose file, restores the settings saved before the update, checks health and reopens the tunnel. Pairing survives a rollback. If you roll back and then update again, the TV may keep one image that the older version uploaded; delete it in the TV’s art collection if you want the space back.

Both commands reuse or reconnect SSH and ask for sudo once.

To reopen the management site without deploying, run **`npm run open`** (`remote.py open`). It reuses or reconnects SSH and opens the same fixed local URL, `http://127.0.0.1:8765/`. No sync, build or container restart is involved.

On a Mac, `open` also copies the management password to the clipboard and opens the browser. The browser shows the sign-in form. Paste the password there. The first time, it asks once for the NAS administrator password, reads `.secrets/web_password` with sudo and saves it in your login Keychain as `art-frames-management`. A password saved before the rename, as `dayframe-management`, still works. It never prints the password. If the password changes on the NAS, run `npm run open -- --new-password`. The SSH connection closes after 12 idle hours, so a closed tunnel can make that URL unavailable even while Art Frames continues running on the NAS. This is a limit of the tunnel, not a sign-out: run `npm run open` again and the browser is still signed in (a sign-in lasts 30 days). Local Astro development is separate and defaults to port 4321.

The update uses one administrator SSH session for all privileged work, so there is at most one sudo prompt per update, even if the build outlasts sudo's credential-cache timeout. When the SSH connection has expired, one SSH login prompt appears first; every later step reuses that connection. Passwords are entered in your terminal and never saved. No key, sudo policy or NAS SSH policy is installed or changed by this command.

## Layouts

Layouts in the layouts repo install automatically. To add your own, add a `[[sources]]` entry.

### The layouts repo

Name the layouts repo once in the ignored `.deploy.local.toml`:

```toml
[app]
local_repo = "../art-frames-layouts"
```

Or fetch it from GitHub at a pinned branch, tag or commit:

```toml
[app]
repo = "brianmuse/art-frames-layouts"
ref = "main"
```

Every `npm run deploy` installs every layout in that repo's `catalog.json`. When the repo gets a new layout, the next deploy installs it. You change no settings.

1. The catalog's `default` is the **first layout**. It installs to `sites/installed` and runs as the `website` service.
2. Every other layout is an **extra layout**. It installs to `sites/apps/<id>` and runs as its own `app-<id>` service.
3. To make another catalog layout the first layout, add `layout = "<id>"` to `[app]`. The rest of the catalog still installs.
4. To leave layouts out, add `skip`:

```toml
[app]
local_repo = "../art-frames-layouts"
skip = ["news"]
```

Local paths resolve from the Art Frames checkout. Commit your changes first: deploy installs only committed files, and it stops when a local Git repository has uncommitted tracked changes. For a single layout with no catalog, use `app = "."` in place of `layout`. Leave out `[app]` to keep the installed layouts as they are.

### Your own layouts

Add one `[[sources]]` entry for each other place that holds layouts. Each source needs a `catalog.json` in the same format as the layouts repo. Every layout in it installs as an extra layout.

A local folder:

```toml
[[sources]]
path = "../my-layouts"
```

A Git repository at a pinned ref:

```toml
[[sources]]
git = "https://github.com/you/my-layouts.git"
ref = "v1.2.0"
skip = ["draft-layout"]
```

1. A `path` inside a Git repository installs only committed files. A plain folder installs as it is. A `path` source takes no `ref`.
2. A `git` source needs a `ref`: a branch, tag or full commit. Deploy fetches that one commit, without tags or submodules. A `github.com` URL uses the GitHub CLI when it is installed. Other URLs use your own Git access. Use an `https://`, `ssh://` or `user@host:path` URL.
3. For a source that you don't own, use a tag or a full commit as the `ref`, not a branch. A branch can change to code you have not read. A full commit must match exactly, or deploy stops.
4. Each source accepts `skip`.
5. The installer checks every source like the layouts repo: catalog format, layout IDs, each `layout.json`, Dockerfile, and layout paths that stay inside the source. It refuses symbolic links and private files such as `.env`, `.secrets` and `node_modules`.
6. Two sources must not give the same layout ID. If they do, deploy stops and names both sources. Add the ID to `skip` in one of them.

Deploy prints the source, the resolved commit and the layouts of each source. It also names each layout that was not installed before, with its source and network access:

```text
New layout: clock from my-layouts (network: none)
```

When the first layout changes, deploy says so. TVs without a chosen layout show the first layout:

```text
The first layout changes from morning to news. TVs without a chosen layout will switch to news.
```

Deploy never runs Git inside a local source. It reads a copy of the repository instead, so commands that the repository's own Git settings name never run on your computer.

### Layout limits and profiles

You can install up to eight layouts. Each TV keeps its own values for at most eight layouts, and each layout is its own container with up to 1 GB of memory. If the catalogs hold more, deploy stops and asks you to add `skip`.

Layouts need a static Compose profile (`compose.static.yaml` or `compose.static.host.yaml`). A browser Compose profile runs no layout. A local run without Docker can use `install-layout.py --first-only`. With a browser profile, deploy installs no layouts, removes the layout settings from `.env`, and prints:

```text
A browser Compose profile runs no layout. Set compose = "compose.static.yaml" in .deploy.local.toml to run them.
```

The rest of the update continues. Art Frames then shows its built-in layout.

### Removed layouts

When a layout leaves its catalog, or you add it to `skip`, the next deploy removes it from the NAS and stops its container. Its saved settings stay in `data/layout-settings`, so the layout keeps them if you install it again from the same source.

While the layout is gone, the TV shows the first layout. If you save that TV's settings, its choice of the removed layout is dropped.

### Where each layout came from

The installer writes `.dayframe-source.json` in each layout folder (`sites/installed` and `sites/apps/<id>`):

```json
{
  "schema_version": 1,
  "id": "news",
  "label": "art-frames-layouts",
  "kind": "repo",
  "ref": "main",
  "commit": "acbdd4a…",
  "catalog_name": "News & weather",
  "path": "layouts/news",
  "installed_at": "2026-11-01T09:30:00+00:00",
  "repository": "brianmuse/art-frames-layouts",
  "url": "https://github.com/brianmuse/art-frames-layouts/tree/acbdd4a…/layouts/news"
}
```

- `kind` is `repo` (the layouts repo), `path` (a local folder) or `git` (a Git URL).
- `label` is the repo name, the folder name, or the Git URL without a user name, password or token.
- `ref` and `commit` are `null` for a plain folder. `ref` is the branch name for a local repository, or `null` when HEAD is detached.
- `catalog_name` is the layout's `name` in its catalog, or the manifest name.
- `path` is the layout folder in its source.
- `installed_at` is when the installer wrote the file, in UTC.
- `repository` and `url` appear only for a source on GitHub. `url` links to the layout folder at the installed commit.

`.dayframe-installed.json` holds only the layout ID and its settings scope, for the runtime.

The file holds no secrets. The management site can read it to show where each layout came from.

### What deploy writes on the NAS

The update writes `sites/compose.apps.yaml` on the NAS from the validated layout profiles (`scripts/app-compose.sh`). Each `app-<id>` service gets the same hardening as `website`: user 10001, a read-only root file system, no capabilities, `no-new-privileges`, and a 32 MB `noexec` `/tmp`. It has its own socket volume, its own read-only `/site-config` and its own `/site-state`. Do not edit this file; the next update replaces it. The update, rollback, `up`, `status` and `logs` commands add it automatically through `scripts/compose-files.sh`. The update stores the extra layouts as `DAYFRAME_APPS=<id>=<scope> ...` in the private `.env`.

Before activation, the update runs the container check (`scripts/check-containers.sh`) for the `website` image and for each `dayframe-app-<id>:local` image. A broken layout image stops the update, and the running version stays as it is.

### Rollback and settings

The installer sends the new layouts to `sites/.incoming`. Unchanged files are linked to the installed copies (`rsync --link-dest`), so they are not sent again. rsync writes each changed file as a new file, so the installed copy does not change. The last step of the transfer writes `sites/.incoming/.complete`. An update moves no layouts without it: it removes a transfer that did not finish and says so. An update that sends no layouts (no `[app]`, or a browser Compose profile) removes `sites/.incoming` too.

Only one update or rollback runs at a time. Each takes the lock folder `.update-lock`. When a second one starts, it stops and prints:

```text
Another update is running. If none is, remove <lock> and try again.
```

The transfer can't take the lock, because the lock needs root. It stops when the lock exists at its start. Don't start a second deploy while the first one asks for the administrator password.

Before it changes anything, the update saves the current `.env`, `sites/compose.apps.yaml` and the list of running layouts in `.previous`, a private folder that only root can read. It also tags each running image as `:previous`.

The update then prepares and builds from `sites/.incoming`. The running controller reads `sites/`, so it keeps the old layouts during the build. `.env` and the override name the final folders, `sites/installed` and `sites/apps`.

Just before the controller stops, the update moves the layouts into place. The layout folders they replace move to `.previous/sites`. The file `.previous/sites/steps` records each move before it is done, so a failure undoes exactly the moves that ran. The update ignores hang-up and interrupt signals during the moves.

Until the new version starts, the update keeps the file `.previous/unfinished`. If an update stops before that point (for example after a crash during the build or the moves), the next update, rollback or `place-apps.sh` first puts back the saved layout folders, `.env` and the layout override. A rollback that stopped while it put the layout folders back is finished the same way. If this is not possible, the run stops and prints:

```text
Could not put back the layout folders. The steps are in <path>. Run the rollback again.
```

The update never deletes the saved layout folders of a move that did not finish. A `.previous/sites` folder without a `steps` file stops the run, because Art Frames can't tell which folders ran. Move the folders you need back to `sites/` by hand, then remove it.

The update writes each restored file to a temporary file first, then moves it into place. A full disk can't leave an empty `.env`.

If the update fails before the new version starts, it puts those files, layout folders and images back and prints:

```text
Nothing changed on the TVs. The previous version is still running.
```

When nothing ran before, it prints `Nothing was started.` instead.

If the controller does not start again after a failure, the update prints `The previous version could not be started again. Run: sudo docker start <id>`.

Just before the new version starts, the update stops the controller by its container ID. The controller is the one container that runs `dayframe:local` or `dayframe:previous`; when more than one does, the update stops before it changes anything. Then it copies `data/settings.json` to `.previous/settings.json`. All Art Frames settings are in that file, including each TV's settings and layout schedule. The update refuses a settings file that is a link or has another hard link. From this point, a lost SSH connection does not stop the update: it ignores the hang-up signal and finishes, or rolls back.

A newer version can save settings that an older version rejects. So a rollback, automatic or with `npm run rollback`, does these steps:

1. It puts back the layout folders saved in `.previous/sites`. When there is no saved copy, it stops if a layout that ran before the update is gone from `sites/`, or if the first layout changed. Redeploy the previous commit instead.
2. It shows the age of the saved settings. When they are more than one hour old, it asks you to type `yes`. The automatic rollback does not ask.
3. It puts back the saved `.env`, the layout override and the `:previous` images.
4. It checks that the settings file can be replaced, stops the controller, puts the saved settings back and renames the saved copy to `settings.restored-<time>.json`. A second rollback does not restore the same settings again.

Settings changes made after the update are lost. Layout settings in `data/layout-settings` do not change.

#### Rolling back past the weekly layout schedule

The weekly layout schedule saves `app_schedule` with a `week` key. Older versions can't read that file. So before its first save in the new shape, the controller keeps one copy of the older file in `data/settings.pre-week.json`. It never replaces that copy.

Images that can read the `week` key have the label `art-frames.settings=week`. The rollback does the steps below only when all of these are true:

- It has no saved settings in `.previous`.
- `data/settings.json` has a `week` key.
- The previous image does not have that label.

Then it does these steps:

1. It moves `data/settings.json` to `data/settings.week-<UTC time>.json`, so the current settings are never lost.
2. When `data/settings.pre-week.json` exists, it puts that copy back as `data/settings.json`. Then it renames the copy to `settings.pre-week.restored-<UTC time>.json`. When the copy is more than one hour old, the rollback asks first, the same as for saved settings. `--yes` skips the question.
3. Without that copy, the older version starts from the installation defaults. Set it up again on the settings page.

When the rollback cannot read the label, it stops and changes nothing. When a step fails, or the rollback is stopped during these steps, it moves the current settings back to `data/settings.json`.

The rollback prints which step it took.

After a healthy update, the update removes layout image tags that nothing uses: the `:local` image of a removed layout, and `:previous` images of layouts that did not run before the update.

First-time hosts still need their fingerprint verified manually and an existing target share; see below for individual setup and troubleshooting commands.

## Individual commands

- `open`: reconnect, open the fixed local management tunnel and, on a Mac, copy the saved management password to the clipboard.
- `update`: complete the normal deployment sequence above; validates local settings and committed sources before connecting.
- `logs [service] [--follow] [--since 24h]`: show recent container logs.
- `rollback`: restart the `:previous` images with the saved files and settings, and check health. See [Rollback and settings](#rollback-and-settings).
- `connect`: authenticate in your own terminal. Starts an SSH control connection in the ignored, mode-0700 `.deploy` directory. It closes after 12 idle hours. Host-key checking remains strict, and agent/X11 forwarding is disabled. For a new host, first connect manually and independently verify its fingerprint; this helper does not automatically accept keys.
- `check`: read-only account/architecture and executable checks, plus verification that the configured parent directory exists. Does not read other shares or require root.
- `sync`: uploads a snapshot of **committed Git source** to the managed `source` child using rsync over SSH. It never sends `.git`, ignored files, or GitHub credentials. It refuses tracked changes until committed. It removes obsolete source files only inside that marked child. Runtime data is kept outside it.
- `plan`: after the first sync, previews source changes with rsync's dry-run mode.
- `prepare`: asks for sudo authentication in your terminal. Passes the renderer mode of your Compose profile. For a browser profile, it removes every layout setting from `.env`. Creates initial config and password only when absent, assigns UID/GID 10001 to the runtime paths that Art Frames owns, and links those paths into the source checkout. Existing private settings, passwords, and data are preserved. Custom container UID/GID values require manual ownership setup; the helper stops rather than guessing.
- `up`: asks for sudo authentication and runs `scripts/build-check.sh`, the same checks as the update: it checks host capabilities before building, builds the chosen renderer's images, verifies real isolated 4K capture on the host for the `website` image and each extra layout image, then starts the project. It also stops the container of a layout that is no longer installed (`--remove-orphans`). A failed host or capture check prevents startup. It does not change Docker socket permissions or host group membership. Select a `compose.static` variant for HTML/CSS rendering on NAS kernels without browser sandbox support; see [renderer choices](renderers.md).
- `status`: shows the project's containers through sudo.
- `tunnel`: exposes management HTTP at `http://127.0.0.1:8765` on your computer. With an existing multiplexed connection it may return immediately; the forward belongs to that connection and closes when it ends or expires. Type the password in the sign-in form; retrieve the generated password from the installation's `.secrets/web_password` through your own administrative terminal. Do not paste it into chat or Git.
- `enable-tunnel`: an **explicit SSH policy change**, only for a host that blocks forwarding. With sudo, it backs up `/etc/ssh/sshd_config`, stages and validates a per-user `AllowTcpForwarding local` rule with `PermitOpen 127.0.0.1:8765`, then reloads the SSH daemon. It does not enable global forwarding, remote forwarding, or additional login accounts. Custom existing policies are refused for manual review. Disconnect and reconnect your SSH control connection afterward; existing sessions retain their prior policy. DSM updates may overwrite this rule. Backups are under the installation's private `ssh-backups` directory.
- `lan` and `lan-off` (`npm run lan`, `npm run lan:off`): open Art Frames on the home network at `http://<NAS IP>:<port>`, or close it again. They change only the `DAYFRAME_LISTEN` and `DAYFRAME_HOSTS` lines in `.env`, then restart the project. Run `npm run deploy` first, so the NAS has the script.
- `rename-check [--dry-run]`: checks the NAS install for the Art Frames rename, prints what stays and what changes, and makes one backup in `<base>/.rename-backup-<time>/`. With `--dry-run` it makes no backup. Run `sync` first. It moves, renames and deletes nothing, except its own partial backup folder when the backup fails. `update` runs the same check with `--auto`: it makes the backup once, before the first update after the rename, and is silent after that.
  - The backup skips SQLite `-wal` and `-shm` files. If a file changes while `tar` reads it, the check only warns.
  - If the backup fails during `update`, the check prints a warning and the update goes on. The next update tries the backup again. A failed backup leaves no partial folder.
  - If the check says STOP for free space, free some space, or skip the backup: on the NAS, run `sudo sh -c 'date -u > <base>/.rename-backup-done'`.
  - The backup folder is readable by root only and holds secrets (`.env`, `.secrets/`, the TV tokens). Use it only for a disaster restore. For a rollback, use `npm run rollback`, which uses `.previous`. Delete the folder after two healthy releases: `sudo rm -rf <base>/.rename-backup-<time>`.
- `disconnect`: closes the temporary control connection. Stop an active tunnel with Ctrl-C first.

The individual commands use SSH BatchMode and will fail if your existing connection/agent cannot authenticate. The combined `update` command reconnects automatically; for troubleshooting commands, re-run `connect` locally when needed. There is no unattended sudo configuration and no SSH key installation.

## Synology source install

This section is for a source install on a Synology. For the simple install with published images, see [Synology installation](synology.md). Many Synology kernels cannot run the Chromium renderer; see [why the HTML/CSS renderer](synology.md#why-the-htmlcss-renderer).

For native Container Manager hosting, use the optional **HTML/CSS** profile: `compose.static.yaml` or `compose.static.host.yaml`. It requires enforced memory limits and uses process-level resource limits without Chromium. The included Astro service fills in live content before rendering, so it does not depend on browser JavaScript. Review the different content and isolation guarantees in [renderer choices](renderers.md). A real capture on the NAS must pass before starting the application.

For development and repeated deployments, prefer the SSH helper on this page. It keeps host details local and ignored, transfers committed source without GitHub credentials on the NAS, and preserves Art Frames data separately. The File Station workflow below remains an alternative. Container Manager creates and requires its own `docker` share; creating a separate services share does not replace that package requirement.

1. Create a shared folder named `services` through **Control Panel → Shared Folder**. Choose the volume intentionally; it need not match the volume holding your private data. Grant access only to the accounts that administer these services.
2. Create a `dayframe` folder inside it and place the application source there. Download the source archive from GitHub on your computer, then upload and extract it with File Station. No GitHub token needs to live on the NAS.
3. Copy `config.example.toml` to `config.local.toml` and `.env.example` to `.env`. Edit your timezone. Leave wake off for the first pairing/upload test.
4. Create `data` and `.secrets` inside the project folder. Make the password file on your computer with `openssl rand -base64 32 > web_password`, then upload it as `.secrets/web_password`. Do not share its contents.
5. Create a `sites` folder for custom website builds. Have a NAS administrator assign UID/GID 10001 ownership of **only** this project's `data`, `.secrets`, and private `config.local.toml`, for example with `sudo chown 10001:10001 <path>` in an administrator terminal. The container needs write access to its own data and read access to its password. Do not relax Docker socket permissions or add a user to a privileged Docker group.
6. In **Container Manager → Project → Create**, select the `services/dayframe` project path and choose `compose.static.yaml` for bridge networking or `compose.static.host.yaml` for LAN wake broadcasts; keep `compose.base.yaml` and `renderer/` beside them. Run the host/container checks described in the renderer guide before starting. The CLI helper performs those checks in order.
7. From your computer, establish a temporary tunnel: `ssh -N -L 8765:127.0.0.1:8765 your-user@your-nas-ip`. Visit `http://127.0.0.1:8765`. Type the locally stored password in the sign-in form.
8. Configure the TV IP and pair while the TV is awake. Test a library image and website preview before enabling automation.

The optional host network enables LAN Wake-on-LAN broadcasts. It does not make the HTTP server public: HTTP binds to the NAS loopback address. The container uses an unprivileged UID, a read-only root filesystem, dropped capabilities, no-new-privileges, and a single writable data mount. It has no Docker socket mount.

### Open a source install on your home network, without the tunnel

The profiles serve Art Frames on the NAS loopback address only. To reach it from any phone or computer on your home network:

1. On your computer, run `npm run deploy`, so the NAS has the current scripts.
2. Run `npm run lan`. Type the NAS administrator password when asked. It adds two lines to `.env` (`DAYFRAME_LISTEN=0.0.0.0` and `DAYFRAME_HOSTS=*`), keeps every other line, and restarts Art Frames. It prints the address to open.
3. Open that address, for example `http://<NAS IP>:8765`, and sign in. One sign-in lasts 30 days on that device.

Without the CLI helper, add the same two lines to `.env` yourself and restart the project in Container Manager.

The traffic is plain HTTP. A person on the same network can read it, including the password when you sign in. Do not forward port 8765 from your router. To close it again, run `npm run lan:off`.

### Source upgrades

Update the source and review release notes, stop the project, make a consistent copy of the Art Frames data, config and secret files, then rebuild the project. `restart: unless-stopped` restarts the process after a NAS reboot without changing its settings. A startup outside the wake grace window does not wake the TV late.

The browser worker has no network and no NAS, config or credential mounts; it shares only its private socket volume. Check that a website capture works before you turn on TV automation. See [Hosting](hosting.md#browser-sandbox) for browser sandbox requirements.

## Names that stay

The product was called Dayframe. A source install, the SSH helper, the TVs and the installed layouts depend on these names, so they keep the old word. The install file (`install/docker-compose.yml`) uses `art-frames` for its project, service and images instead. `art_frames/names.py` lists them, and `tests/test_frozen_names.py` fails when one changes.

- The NAS folder `<your-share>/dayframe/` and the marker files `.dayframe-managed`, `.dayframe-layout-managed` and `.dayframe-apps-managed`.
- The Docker project `dayframe`, the service `dayframe` and the images `dayframe:local`, `dayframe-renderer*`, `dayframe-website` and `dayframe-app-<id>`. A new project name would start a second controller and leave rollback with no `:previous` images.
- The `DAYFRAME_*` keys in `.env` and the Compose files.
- The Samsung TV client name `Dayframe`. A new name makes each TV ask to pair again.
- The layout protocol: `http://dayframe.local`, `/dayframe.json`, `/dayframe-art.jpg`, `data-dayframe-ready`, `/run/dayframe-site` and `/run/dayframe-apps`, and `.dayframe-source.json`.
- The HTTP Basic username `dayframe`, for scripts. The new username `art-frames` also works. Browsers use the sign-in form, which asks only for the password.

## Host layout

```text
<your-share>/dayframe/
  .dayframe-managed       # Marker for this helper's dedicated source tree
  source/                # Committed source only, mirrored on each sync
  data/                  # Database, artwork, runtime settings and TV tokens
  sites/                 # Private compiled websites
  .secrets/web_password   # Generated once, mode 0600
  config.local.toml       # Private defaults, created once
  .env                   # Private deployment settings, created once
```

The source checkout contains links to the five runtime paths; rsync explicitly excludes those links. Containers retain the existing isolation and bind only Art Frames' paths. The browser worker has only its private socket volume. Preparation changes ownership only on named Art Frames paths, never recursively across a share.

For an update, commit reviewed changes and run `npm run deploy`. Source synchronization is not atomic; do not sync while another deployment is building. Running containers use their existing built images until replaced. Back up runtime state consistently as described in [hosting](hosting.md#updates). This helper is not a backup or a schema-migration system.

## Install layouts on your computer

Use Python 3.12+, Git and the GitHub CLI. With Art Frames stopped, install the layout catalog:

```sh
uv sync --frozen
uv run python scripts/install-layout.py
```

The installer installs every layout in the catalog. The catalog default is the first layout, in the managed, ignored `sites/installed` directory. Each other layout goes to `sites/apps/<id>` and runs in its own container. The installer resolves the Git revision to a fixed commit and copies only committed layout source. Private GitHub access stays on your computer. For local development:

```sh
uv run python scripts/install-layout.py morning --local-repo ../art-frames-layouts
```

`morning` picks the first layout. The other catalog layouts install too. Add `--skip <id>` to leave a layout out. Extra layouts need a static Compose profile. A browser Compose profile runs no layout. A local run without Docker can install only the first layout:

```sh
uv run python scripts/install-layout.py --local-repo ../art-frames-layouts --first-only
```

An independent layout can supply the same manifest and Dockerfile, without a catalog:

```sh
uv run python scripts/install-layout.py --local-repo ../my-screen --app .
```

Layouts are trusted server code. Review a source before you install it. The catalog and manifest format can support a future picker, but no picker UI exists yet. See [layout packages and secrets](layouts.md).

The remote install prepares only paths that Art Frames owns, and asks for sudo in your own terminal. Source sync does not copy runtime secrets. On a Synology, use an HTML/CSS Compose profile for the Astro layouts: the lightweight renderer receives complete server-rendered HTML.

## Separate layout installation

For the normal path, configure `[app]` once and run `npm run deploy`. For a separate manual install, sync the new committed core, then run:

```sh
.venv/bin/python scripts/install-layout.py --local-repo ../art-frames-layouts --remote
.venv/bin/python scripts/remote.py up
```

`--remote` sends the layouts to `sites/.incoming`. Then `scripts/place-apps.sh` takes the update lock and prepares the runtime from `sites/.incoming`. Only when that works does it move the layouts into place. It moves each replaced folder to `.replaced-sites` and removes it only after the new folder is in place. When a move fails, it puts the installed layouts back. When it stopped halfway, the next run puts them back first. `--first-only` does not work with `--remote`, because it would remove the extra layouts on the NAS.

The installer installs the whole catalog. Give a catalog ID to choose the first layout, and `--skip <id>` to leave a layout out. For extra sources, pass `--plan` with the JSON that `remote.py update` builds: `{"layouts_repo": {...}, "sources": [...]}`. Add `--check` to check the catalogs and layouts without installing anything. The installer transfers committed layout source separately; ordinary core sync deliberately excludes it. Preparation applies a validated narrow layout profile and assigns each layout's settings directory to UID 10001. No runtime secret is copied from the layout source. See [layout packages](layouts.md).

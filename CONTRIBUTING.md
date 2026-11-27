# Contributing

Thank you for helping with Art Frames. This page tells you what changes we welcome, how to set up the project, and which checks a pull request must pass.

Art Frames has three parts:

- a Python controller (`art_frames/`) that talks to the TVs and serves the management pages,
- a renderer (`renderer/`) that turns a web page into a TV image, with no network,
- a replaceable website (`website/`), a small Astro starter.

Layouts (the pages that show on the TV, such as the calendar or the weather) live in a separate repository.

## Contribute a layout

Did you make a layout that other households could use? Share it. Send layouts to [art-frames-layouts](https://github.com/brianmuse/art-frames-layouts), not to this repository. Its [contributing guide](https://github.com/brianmuse/art-frames-layouts/blob/main/CONTRIBUTING.md) tells you how.

Open an issue here only when a layout needs something new from the host, for example a new snapshot field or setting type. The host contract is in [layout packages](docs/layouts.md) and [the website contract](docs/website.md).

## What we welcome

- Bug fixes, with a test that fails before the fix.
- TV and host compatibility reports. Use the **TV compatibility report** issue form.
- Clearer documentation and UI text.
- Small improvements that fit the [project scope](#project-scope).

Before you start:

1. **Open an issue first for a big change.** A big change adds a feature, a setting, a dependency, a service or a new source, or changes stored data. Agree on the plan before you write the code.
2. **Make one change per pull request.** Do not mix a fix with a refactor or a dependency update.
3. **Keep private data out.** Do not add IP addresses, MAC addresses, tokens, passwords, setup codes, calendar links, real names or household screenshots to code, fixtures, tests, docs, issues or pull requests. Use made-up values, such as `192.0.2.10` and "Living room".

To report a security problem, do not open an issue. See [security](SECURITY.md).

## Set up

You need Python 3.11 or later, [uv](https://docs.astral.sh/uv/), Node.js 24 and npm. CI uses Python 3.12 and Node.js 24.

```sh
uv sync --frozen --group renderer
npm --prefix website ci
npm --prefix website run build
```

## Run Art Frames on your computer

```sh
uv run art-frames --demo
```

Open `http://127.0.0.1:8765`. Demo mode uses sample data, binds only to loopback and sends no commands to a TV. Its data goes to the ignored `data/` folder.

To work on the starter website with live reload, run `npm run dev`.

To work on a layout, clone [art-frames-layouts](https://github.com/brianmuse/art-frames-layouts) next to this checkout and follow its README.

To deploy a source install to a NAS, see [deployment from your computer](docs/cli-deployment.md).

## Checks

Run these before you open a pull request:

```sh
uv run ruff check .
uv run pytest
```

CI also runs the browser render tests. To run them on your computer:

```sh
uv run playwright install chromium
DAYFRAME_TEST_RENDER=1 uv run pytest
```

CI then checks that the lock files match `uv.lock`, checks the Compose files, builds the images and runs `scripts/check-host.sh` and `scripts/check-containers.sh`. See `.github/workflows/checks.yml`.

### Tests never contact real devices

**A test must never contact a real TV, presence sensor or NAS.** Use demo mode, a fake or a monkeypatched adapter. A test must pass on a computer with no TV on the network.

A check against real hardware is a manual step, not a test. Do it by hand and write the result in the pull request: the TV model, the firmware and the exact wake and sleep conditions.

## Rules for code changes

- **Schedules.** Test the boundaries, the disabled state, sleep and resume, and a restart.
- **Settings.** Add a new setting to the Pydantic schema, `config.example.toml`, [the configuration reference](docs/configuration.md) and the browser form.
- **Stored data.** A change to stored data needs a migration plan. Describe it in the pull request.
- **Artwork sources.** A source adapter needs a public-domain check and a fixed list of allowed hosts. Keep the source URL and rights information with each imported artwork.
- **Renderer.** The renderer stays networkless, with no app, NAS, config or credential mounts. Never fix a sandbox failure with a privileged container or `SYS_ADMIN`. A change to the private socket messages must update both sides and their contract tests.
- **Website.** Astro is an example, not a requirement. Keep the website replaceable and the artwork context opt-in. Test local assets, path and symlink rejection, size limits, deadlines and a real 4K render.
- **UI.** Describe the user's action, not how the code works. Test an empty library, an unavailable provider, a rejected image and a narrow screen. Styles live in `art_frames/static/style.css`. Bundle scripts and fonts; do not load them from a CDN.

## Dependencies and lock files

`uv.lock` pins every Python dependency. The Docker images install hash-checked exports of it. CI fails when an export does not match `uv.lock`.

After you change a dependency in `pyproject.toml`, run:

```sh
uv lock
uv export --frozen --no-header --no-dev --no-emit-project --format requirements-txt > requirements.lock
uv export --frozen --no-header --only-group renderer --no-emit-project --format requirements-txt > renderer/requirements.lock
uv export --frozen --no-header --only-group static-renderer --no-emit-project --format requirements-txt > renderer/requirements.static.lock
```

Commit all four files. Explain in the pull request why the project needs the new dependency.

## Container checks

To check the images on your computer, build them with the names that the check expects, then run it:

```sh
sh scripts/check-host.sh
docker build -t dayframe:test .
docker build -f renderer/Dockerfile -t dayframe-renderer:test .
sh scripts/check-containers.sh
```

CI also checks the HTML/CSS (static) images. See the last steps in `.github/workflows/checks.yml`.

`check-containers.sh` checks the bundled website through the container renderer and the private socket. It creates and removes temporary containers and a socket-only volume. The containers have no network and no host data. No TV is contacted. Keep this check passing with the production sandbox on.

## Write in plain language

Write docs, UI text, commit messages and pull request descriptions in plain language:

- Use short sentences and common words.
- Use the active voice. Write instructions as commands: "Run the check", not "The check should be run".
- Use one word for one thing. Say "layout", not "app", in anything a user reads.

## Project scope

Art Frames supports up to eight TVs per installation, replaceable layouts, three museum sources and local file storage. See [the architecture](docs/architecture.md#multiple-tvs-and-reusable-sites). You can add display adapters and artwork sources through the existing seams.

Do not add cloud accounts, AI services, telemetry or bulk downloads of museum collections.

## Releases

The maintainer makes releases. See [releasing](docs/releasing.md). Before a release, check the staged files for credentials and runtime data. Do not ship private calendar examples, NAS exports, screenshots of household events or a filled art catalog.

## Code of conduct

Everyone who takes part follows the [code of conduct](CODE_OF_CONDUCT.md).

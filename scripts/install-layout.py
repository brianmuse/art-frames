"""Install every layout in the layouts repo's catalog, plus the layouts of any extra source.

Run locally with Art Frames stopped, or with --remote through remote.py update.
The catalog default (or the chosen layout) is the first layout; every other layout is an extra layout.

Private GitHub access stays on this computer. Runtime secrets are never copied
from a source or sent to GitHub.
"""

import argparse
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from art_frames.apps import ARTWORK_ID  # noqa: E402
from art_frames.config import ConfigStore, private_write  # noqa: E402
from art_frames.layouts import Manifest  # noqa: E402
from art_frames.names import (  # noqa: E402
    APPS_MARKER,
    APPS_MARKER_FILE,
    EARLIER_SCOPES_FILE,
    INSTALLED_FILE,
    LAYOUT_MARKER,
    LAYOUT_MARKER_FILE,
    READY_SELECTOR,
    SOURCE_FILE,
    SOURCE_MARKER,
    SOURCE_MARKER_FILE,
)
from scripts.app_sources import (  # noqa: E402
    APP_ID,
    BROWSER_NOTE,
    COMMIT,
    DEFAULT_REPO,
    InstallError,
    check_ref,
    clone,
    git,
    git_env,
    git_location,
    github_repo,
    origin_url,
    safe_url,
    validate_plan,
)

PROFILE_FILE = ".install-profile"
# GitHub repositories that were renamed: the current name and its earlier names. A layout from
# the current name keeps the settings it saved under an earlier name of the same owner's
# repository. A different source that reuses a layout ID never gets those settings.
RENAMED_REPOSITORIES = {"art-frames-layouts": ("dayframe-layouts",)}
# The one-line profile that scripts/app-profile.sh reads, in this field order.
PROFILE_FIELDS = ("id", "network", "memory_mb", "entry", "scope", "artwork")
# The runtime keeps per-TV values for at most eight layouts (Config.app_settings), and each
# layout is its own container with up to 1 GB of memory on a small NAS. Use skip for more.
MAX_APPS = 8
RESERVED = {
    ".env",
    ".secrets",
    ".git",
    "node_modules",
    PROFILE_FILE,
    EARLIER_SCOPES_FILE,
    INSTALLED_FILE,
    LAYOUT_MARKER_FILE,
    SOURCE_FILE,
}


def unpack(raw, destination, *, strip_root=False):
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError("Layout archives may contain only regular files and directories")
            parts = Path(member.name).parts
            if Path(member.name).is_absolute() or ".." in parts:
                raise ValueError("Layout archive escapes its directory")
            if strip_root:
                member.name = str(Path(*parts[1:]))
        archive.extractall(destination, filter="data")


@dataclass(frozen=True)
class Origin:
    """Where one layout comes from. The base origin of a source has an empty app_path."""

    kind: str  # "repo" (the layouts repo), "path" (a local folder) or "git" (a Git URL)
    label: str  # shown to people; never holds a URL credential or a full local path
    ref: str | None
    commit: str | None
    source_id: str  # stable identity of the source, part of each layout's settings scope
    app_path: str = ""  # the layout folder within the source
    catalog_name: str = ""
    repository: str | None = None  # owner/name, only when the source resolves to GitHub
    repo_prefix: str = ""  # the source folder within its repository, for the layout's link

    def settings_scope(self, manifest_id):
        identity = self.source_id + ":" + self.app_path
        return manifest_id[:40] + "-" + hashlib.sha256(identity.encode()).hexdigest()[:32]

    def earlier_scopes(self, manifest_id):
        """The scopes this layout had before its GitHub repository was renamed (RENAMED_REPOSITORIES)."""
        owner, _, name = self.source_id.removeprefix("github:").partition("/")
        if not self.source_id.startswith("github:") or not owner:
            return []
        return [
            replace(self, source_id=f"github:{owner}/{earlier}").settings_scope(manifest_id)
            for earlier in RENAMED_REPOSITORIES.get(name, ())
        ]

    def link(self):
        """An https link to the layout's folder at its commit, built only from the GitHub slug."""
        if not self.repository or not self.commit:
            return None
        path = "/".join(part for part in (self.repo_prefix, self.app_path) if part)
        return f"https://github.com/{self.repository}/tree/{self.commit}/{path}"


class RuntimePaths(NamedTuple):
    sites: Path
    data: Path
    env: Path
    config: Path


class Staged(NamedTuple):
    scope: str
    settings: Path
    state: Path

    def owned(self):
        """The runtime paths the container user owns, in the order the installer always used."""
        return [self.settings.parent, self.settings, self.state.parent, self.state, self.settings / "config.json"]


def is_managed(directory, marker, value):
    """True when directory/marker is a regular file (not a link) that holds exactly value."""
    path = Path(directory) / marker
    return path.is_file() and not path.is_symlink() and path.read_text().strip() == value


def read_manifest(package):
    return Manifest.model_validate_json((package / "layout.json").read_bytes())


def runtime_paths(root):
    """sites, data, .env and config.local.toml, following only this installation's managed links."""
    managed = root.name == "source" and is_managed(root.parent, SOURCE_MARKER_FILE, SOURCE_MARKER)

    def runtime(name):
        path = root / name
        if path.is_symlink():
            expected = root.parent / name
            if (
                not managed
                or path.readlink() != Path("..") / name
                or expected.is_symlink()
                or path.resolve() != expected
            ):
                raise ValueError("Refusing an unmanaged runtime mapping")
            return expected
        return path

    return RuntimePaths(*(runtime(name) for name in ("sites", "data", ".env", "config.local.toml")))


def source_record(manifest, origin):
    """The public record of where an installed layout came from, for the Layouts page."""
    record = {
        "schema_version": 1,
        "id": manifest.id,
        "label": origin.label,
        "kind": origin.kind,
        "ref": origin.ref,
        "commit": origin.commit,
        "catalog_name": origin.catalog_name or manifest.name,
        "path": origin.app_path,
        "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if origin.repository:
        record["repository"] = origin.repository
        if origin.link():
            record["url"] = origin.link()
    return record


def profile_line(manifest, scope):
    """The .install-profile line, in PROFILE_FIELDS order."""
    values = {
        "id": manifest.id,
        "network": manifest.network,
        "memory_mb": manifest.memory_mb,
        "entry": manifest.entry,
        "scope": scope,
        "artwork": int(manifest.artwork),
    }
    return " ".join(str(values[field]) for field in PROFILE_FIELDS) + "\n"


def unmanaged(path):
    return InstallError(f"{path} holds files that Art Frames did not install. Move them away, then install again")


# The built-in Artwork's ID. Art Frames shows it where no layout is scheduled.
RESERVED_IDS = frozenset({ARTWORK_ID})


def check_id(app_id, origin):
    if app_id in RESERVED_IDS:
        raise InstallError(f"{origin.label}: layout ID {app_id} is reserved for the built-in Artwork. "
                           "Give the layout another ID in its layout.json")


def check_package(package, origin, manifest):
    """The package rules: a Dockerfile, and no links, private files or files reserved for Art Frames."""
    where = f"{origin.label}: layout {manifest.id} ({origin.app_path or '.'})"
    check_id(manifest.id, origin)
    if not (package / "Dockerfile").is_file():
        raise InstallError(f"{where} has no Dockerfile. A layout server needs one")
    for path in package.rglob("*"):
        relative = path.relative_to(package)
        if path.is_symlink() or any(
            part in RESERVED or part.startswith(".env.") and part != ".env.example" for part in relative.parts
        ):
            raise InstallError(
                f"{where} contains {relative.as_posix()}, which is a link, private or reserved for Art Frames. "
                "Remove it from the layout"
            )


def _stage_package(package, origin, manifest, target, data):
    """Check one layout package, copy it to target and create its private settings and state."""
    check_package(package, origin, manifest)
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("Refusing a symlinked installation directory")
    if target.exists() and any(target.iterdir()) and not is_managed(target, LAYOUT_MARKER_FILE, LAYOUT_MARKER):
        raise unmanaged(target)
    scope = origin.settings_scope(manifest.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(package, target)
    (target / LAYOUT_MARKER_FILE).write_text(LAYOUT_MARKER + "\n")
    (target / INSTALLED_FILE).write_text(json.dumps({"id": manifest.id, "settings_scope": scope}, indent=2) + "\n")
    (target / SOURCE_FILE).write_text(json.dumps(source_record(manifest, origin), indent=2) + "\n")
    # Both layout kinds get the same profile. The shared Compose generator reads it for extra
    # layouts, and prepare-remote.sh reads it for the first layout on the NAS.
    (target / PROFILE_FILE).write_text(profile_line(manifest, scope))
    earlier = origin.earlier_scopes(manifest.id)
    if earlier:
        (target / EARLIER_SCOPES_FILE).write_text("".join(name + "\n" for name in earlier))
    settings = data / "layout-settings" / scope
    if any(path.is_symlink() for path in (data, settings.parent, settings)):
        raise ValueError("Refusing symlinked runtime configuration")
    keep_settings(data, target, manifest.id)
    settings.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.chmod(0o700)
    state = data / "layout-state" / scope
    if any(path.is_symlink() for path in (state.parent, state)):
        raise ValueError("Refusing symlinked layout state")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    return Staged(scope, settings, state)


def keep_settings(data, target, app_id):
    """Copy a layout's saved settings from its scope under an earlier name of its source.

    It runs only when the layout's current scope has no settings, and only for the scopes in
    the layout's .earlier-scopes file. scripts/keep-layout-settings.sh does the copy, and
    prepare-remote.sh runs the same script on the NAS. A failure only prints a warning.
    """
    result = subprocess.run(
        ["sh", str(SCRIPTS / "keep-layout-settings.sh"), str(data), str(target)],
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.returncode != 0:
        print(f"Could not restore the saved settings of layout {app_id} from an earlier install.", file=sys.stderr)


def install_first(root, package, origin):
    """Install the first layout: sites/installed, the `website` service and the DAYFRAME_LAYOUT_* keys."""
    paths = runtime_paths(root)
    manifest = read_manifest(package)
    staged = _stage_package(package, origin, manifest, paths.sites / "installed", paths.data)
    configs = ConfigStore(paths.data, paths.config if paths.config.exists() else None)
    configs.mutate(
        lambda raw: raw["site"].update(
            path="",
            entry=manifest.entry,
            providers=["art"] if manifest.artwork else [],
            ready_selector=READY_SELECTOR,
        )
    )
    content = paths.env.read_text() if paths.env.exists() else (root / ".env.example").read_text()
    changes = {
        "DAYFRAME_LAYOUT_ID": manifest.id,
        "DAYFRAME_LAYOUT_CONFIG_ID": staged.scope,
        "DAYFRAME_LAYOUT_ARTWORK": "1" if manifest.artwork else "0",
        "DAYFRAME_LAYOUT_ENTRY": manifest.entry,
        "DAYFRAME_LAYOUT_MANIFEST": "/sites/installed/layout.json",
        "DAYFRAME_SITE_BUILD_CONTEXT": "./sites/installed",
        "DAYFRAME_SITE_DOCKERFILE": "Dockerfile",
        "DAYFRAME_SITE_NETWORK": manifest.network,
        "DAYFRAME_SITE_MEMORY": f"{manifest.memory_mb}m",
    }
    lines = [line for line in content.splitlines() if line.partition("=")[0] not in changes]
    private_write(
        paths.env,
        (
            "\n".join(lines) + "\n" + "\n".join(f"{key}={value}" for key, value in changes.items()) + "\n"
        ).encode(),
    )
    own(
        paths.env,
        [
            paths.data,
            staged.settings.parent,
            staged.settings,
            staged.state.parent,
            staged.state,
            paths.data / "settings.json",
            paths.data / "legacy-dashboard.json",
            staged.settings / "config.json",
        ],
    )
    return manifest


def install_extra(root, package, origin):
    """Install one extra layout to sites/apps/<id>. It runs as its own `app-<id>` service.

    The core site settings and the first layout's .env keys stay as they are.
    """
    paths = runtime_paths(root)
    manifest = read_manifest(package)
    if not APP_ID.fullmatch(manifest.id):
        raise InstallError(f"{origin.label}: layout ID {manifest.id} must start with a letter to run as an extra layout")
    target = paths.sites / "apps" / manifest.id
    if target.parent.is_symlink():
        raise ValueError("Refusing a symlinked installation directory")
    managed_parent(target.parent)
    staged = _stage_package(package, origin, manifest, target, paths.data)
    own(paths.env, staged.owned())
    return manifest


def own(env, paths):
    """As root, give layout-owned runtime paths to the container user from .env."""
    if os.geteuid() != 0:
        return
    lines = env.read_text().splitlines() if env.exists() else []
    uid = int(next((line.split("=", 1)[1] for line in lines if line.startswith("DAYFRAME_UID=")), "10001"))
    gid = int(next((line.split("=", 1)[1] for line in lines if line.startswith("DAYFRAME_GID=")), "10001"))
    for path in paths:
        if path.is_symlink():
            raise ValueError("Refusing symlinked runtime configuration")
        if path.exists():
            os.chown(path, uid, gid)


def managed_parent(apps):
    """sites/apps holds only installer-managed extra layouts."""
    if apps.is_symlink() or (apps / APPS_MARKER_FILE).is_symlink():
        raise ValueError("Refusing a symlinked installation directory")
    if apps.exists() and any(apps.iterdir()) and not is_managed(apps, APPS_MARKER_FILE, APPS_MARKER):
        raise unmanaged(apps)
    apps.mkdir(parents=True, exist_ok=True)
    (apps / APPS_MARKER_FILE).write_text(APPS_MARKER + "\n")


def check_app_count(count):
    if not 1 <= count <= MAX_APPS:
        raise InstallError(
            f"This would install {count} layouts. Art Frames runs one to {MAX_APPS}. "
            "Add skip = [...] to the deploy settings to leave some out."
        )


def install_all(root, items):
    """Install the first layout and each extra layout, remove extra layouts that are gone, and write the override.

    items is a list of (package, Origin); the first is the first layout.
    """
    check_app_count(len(items))
    ids = [read_manifest(package).id for package, _ in items]
    if len(ids) != len(set(ids)):
        raise InstallError("Each layout needs its own ID")
    for (_, origin), app_id in zip(items, ids, strict=True):
        check_id(app_id, origin)
    manifests = [install_first(root, *items[0])]
    apps = runtime_paths(root).sites / "apps"
    if len(items) > 1 or apps.exists():
        managed_parent(apps)
        for child in apps.iterdir():
            if child.name.startswith(".") or child.name in ids[1:]:
                continue
            if child.is_symlink() or not child.is_dir() or not is_managed(child, LAYOUT_MARKER_FILE, LAYOUT_MARKER):
                raise unmanaged(child)
            shutil.rmtree(child)
    for package, origin in items[1:]:
        manifests.append(install_extra(root, package, origin))
    compose_apps(root)
    return manifests


def compose_apps(root):
    """Write sites/compose.apps.yaml with the shared generator and record DAYFRAME_APPS in .env."""
    paths = runtime_paths(root)
    if not paths.sites.exists():
        return ""
    value = subprocess.run(
        ["sh", str(SCRIPTS / "app-compose.sh"), str(paths.sites.parent)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    content = paths.env.read_text() if paths.env.exists() else ""
    lines = content.splitlines()
    current = next((line.split("=", 1)[1] for line in reversed(lines) if line.startswith("DAYFRAME_APPS=")), None)
    if (current or "") != value:
        lines = [line for line in lines if not line.startswith("DAYFRAME_APPS=")]
        private_write(paths.env, ("\n".join([*lines, "DAYFRAME_APPS=" + value]) + "\n").encode())
    return value


# --- What changes for people ---------------------------------------------------------------


def installed_apps(sites):
    """(first layout ID or None, set of every installed layout ID) in a local sites folder."""

    def app_id(folder):
        for name in (SOURCE_FILE, INSTALLED_FILE):
            path = folder / name
            if path.is_file() and not path.is_symlink():
                try:
                    return json.loads(path.read_text()).get("id")
                except ValueError:
                    continue
        return None

    first = app_id(sites / "installed")
    apps = sites / "apps"
    extras = {child.name for child in apps.iterdir() if child.is_dir() and app_id(child)} if apps.is_dir() else set()
    return first, extras | ({first} if first else set())


def report_changes(previous, items, report=print):
    """Say when the first layout changes, and name each layout that was not installed before.

    previous is None when the current layouts could not be read.
    """
    if previous is None:
        report("Could not read the layouts on the NAS now, so new layouts are not named.", flush=True)
        return
    first, before = previous
    manifests = [(read_manifest(package), origin) for package, origin in items]
    new_first = manifests[0][0].id
    if first and first != new_first:
        report(
            f"The first layout changes from {first} to {new_first}. "
            f"TVs without a chosen layout will switch to {new_first}.",
            flush=True,
        )
    for manifest, origin in manifests:
        if manifest.id not in before:
            report(f"New layout: {manifest.id} from {origin.label} (network: {manifest.network})", flush=True)


REMOTE_APPS = """cd {sites} 2>/dev/null || exit 0
if test -f installed/.install-profile; then printf 'first %s\\n' "$(cut -d' ' -f1 installed/.install-profile)"; fi
for folder in apps/*/; do
    if test -f "$folder.install-profile"; then printf 'extra %s\\n' "$(cut -d' ' -f1 "$folder.install-profile")"; fi
done
exit 0
"""


def remote_installed_apps(ssh, remote, base):
    """(first layout ID or None, every installed layout ID) on the NAS, or None when SSH fails."""
    result = subprocess.run(
        [*ssh, "-o", "BatchMode=yes", remote, REMOTE_APPS.format(sites=shlex.quote(base + "/sites"))],
        capture_output=True,
        text=True,
    )
    if result is None or result.returncode != 0:
        return None
    first, every = None, set()
    for line in (result.stdout or "").splitlines():
        kind, _, app_id = line.partition(" ")
        if kind in ("first", "extra") and APP_ID.fullmatch(app_id):
            every.add(app_id)
            first = app_id if kind == "first" else first
    return first, every


def install_remote(items, *, prepare=True):
    """Transfer the layouts to the NAS over the existing SSH session. items[0] is the first layout.

    The layouts go to sites/.incoming, never straight into sites/, so the running version keeps
    its layouts until an administrator script moves them into place: update-remote.sh, which
    keeps the replaced layouts for a rollback, or place-apps.sh for a separate install.
    The last step writes .incoming/.complete; those scripts move nothing without it.
    --link-dest assumes GNU rsync (version 3) on the NAS.
    rsync reuses unchanged files of the installed layouts (--link-dest). rsync writes each new
    or changed file as a new file, so a linked file of a running layout never changes.
    All extra layouts go in one rsync with --delete, so a layout that is no longer in a catalog
    is also removed. A browser Compose profile gets no layouts.

    The transfer does not hold the update lock, because the lock needs root. It stops when
    an update or rollback holds the lock at its start.
    """
    from scripts.remote import compose_mode, ssh_options, target

    config = tomllib.loads((ROOT / ".deploy.local.toml").read_text())
    remote, base, _, compose = target(config)
    mode = compose_mode(compose)
    if mode == "browser":
        print(BROWSER_NOTE, flush=True)
        return
    ssh = ssh_options(config)
    report_changes(remote_installed_apps(ssh, remote, base), items)
    # Validate the same package rules using a disposable local installation.
    with tempfile.TemporaryDirectory(prefix="dayframe-installed-") as directory:
        root = Path(directory)
        (root / ".env.example").write_text("DAYFRAME_UID=10001\nDAYFRAME_GID=10001\n")
        manifests = install_all(root, items)
        extras = items[1:]
        staged = root / "sites/installed"
        incoming = base + "/sites/.incoming"
        guard = f"""set -eu
base={shlex.quote(base)}
test "$(cd "$(dirname "$base")" && pwd -P)" = "$(dirname "$base")"
test ! -L "$base"
test ! -L "$base/sites"
test ! -L "$base/.dayframe-managed"
test "$(cat "$base/.dayframe-managed")" = {SOURCE_MARKER}
test -f "$base/source/scripts/install-layout.py" || {{ echo 'Sync current Art Frames source before installing a layout' >&2; exit 1; }}
if test -e "$base/.update-lock" || test -L "$base/.update-lock"; then
    echo "Another update is running. If none is, remove $base/.update-lock and try again." >&2
    exit 1
fi
mkdir -p "$base/sites"
test ! -L "$base/sites/installed"
if test -d "$base/sites/installed" && test -n "$(ls -A "$base/sites/installed")"; then
    test ! -L "$base/sites/installed/.dayframe-layout-managed"
    test "$(cat "$base/sites/installed/.dayframe-layout-managed")" = {LAYOUT_MARKER}
fi
test ! -L "$base/sites/apps"
if test -d "$base/sites/apps" && test -n "$(ls -A "$base/sites/apps")"; then
    test ! -L "$base/sites/apps/.dayframe-apps-managed"
    test "$(cat "$base/sites/apps/.dayframe-apps-managed")" = {APPS_MARKER}
fi
# A fresh staging folder, so it holds only this transfer.
test ! -L "$base/sites/.incoming"
rm -rf "$base/sites/.incoming"
mkdir -p "$base/sites/.incoming/installed"
"""
        subprocess.run([*ssh, "-o", "BatchMode=yes", remote, guard], check=True)
        rsync = [
            "rsync",
            "-rlpt",
            "--delete",
            "--rsync-path=" + config.get("rsync_path", "rsync"),
            "-e",
            shlex.join([*ssh, "-o", "BatchMode=yes"]),
        ]
        # --link-dest is relative to the destination folder: sites/.incoming/<name>/.
        subprocess.run(
            [*rsync, "--link-dest=../../installed", str(staged) + "/", remote + ":" + incoming + "/installed/"],
            check=True,
        )
        sent = ["installed"]
        # One rsync for all extra layouts, so a removed layout is pruned. Skipped for a single layout
        # on a host that never had extra layouts, so the single-layout form changes nothing new.
        present = subprocess.run([*ssh, "-o", "BatchMode=yes", remote, "test -d " + shlex.quote(base + "/sites/apps")])
        if extras or present.returncode == 0:
            apps = root / "sites/apps"
            apps.mkdir(parents=True, exist_ok=True)
            (apps / APPS_MARKER_FILE).write_text(APPS_MARKER + "\n")
            subprocess.run(
                [*rsync, "--link-dest=../../apps", str(apps) + "/", remote + ":" + incoming + "/apps/"], check=True
            )
            sent.append("apps")
        # The last step: mark the transfer complete, with the names it holds.
        complete = shlex.quote(incoming + "/.complete")
        subprocess.run(
            [*ssh, "-o", "BatchMode=yes", remote,
             f"printf '%s\\n' {' '.join(sent)} > {complete}.tmp && mv {complete}.tmp {complete}"],
            check=True,
        )
        if prepare:
            subprocess.run(
                [
                    *ssh,
                    "-o",
                    "BatchMode=yes",
                    "-t",
                    remote,
                    "sudo sh " + shlex.quote(base + "/source/scripts/place-apps.sh") + " " + mode,
                ],
                check=True,
            )
    names = ", ".join(item.name for item in manifests)
    if prepare:
        print(f"Installed {names} source and prepared the private runtime directories on the NAS.")
        print("Run scripts/remote.py up to build, check and activate the updated containers.")
    else:
        print(f"Transferred {names} source. Runtime preparation and activation are pending.")


# --- Fetching sources -------------------------------------------------------------------
#
# Each fetch function unpacks one source into `work` and returns a Source. Committed Git
# content comes from a bare clone or an archive, never from Git run in the source itself.


class Source(NamedTuple):
    catalog_dir: Path  # the folder that holds catalog.json
    archive_root: Path  # layout paths in the settings scope are relative to this folder
    origin: Origin  # the source's base origin, without a layout path


def _branch(clone_dir):
    result = subprocess.run(
        ["git", "-C", str(clone_dir), "symbolic-ref", "-q", "--short", "HEAD"],
        capture_output=True,
        text=True,
        env=git_env(),
        cwd="/",
    )
    return result.stdout.strip() or None


def _local_identity(local, common):
    """The settings identity of a local checkout: its GitHub repository, or a hash of its path."""
    repository = github_repo(origin_url(common))
    if repository:
        return "github:" + repository.lower(), repository
    # Path is hashed; no operator path or remote credentials enter the package.
    return "local:" + hashlib.sha256(str(local.resolve()).encode()).hexdigest(), None


def _snapshot(local, work):
    """Clone the repository that holds `local`. Returns (clone, commit, branch, top, common)."""
    if not local.is_dir():
        raise InstallError(f"The folder {local.name} does not exist")
    location = git_location(local)
    if location is None:
        raise InstallError(f"{local.name} is not in a Git repository")
    top, common = location
    clone_dir = clone(top, work.parent / (work.name + ".git"))
    commit = git(clone_dir, "rev-parse", "HEAD", text=True).strip()
    return clone_dir, commit, _branch(clone_dir), top, common


def fetch_local_repo(spec, work):
    """The layouts repo from a local Git repository, at its committed HEAD."""
    local = Path(spec["local_repo"]).resolve()
    clone_dir, commit, branch, top, common = _snapshot(local, work)
    prefix = local.relative_to(top).as_posix()
    prefix = "" if prefix == "." else prefix
    # A subfolder exports only its own files, as git archive did there before.
    unpack(git(clone_dir, "archive", f"{commit}:{prefix}" if prefix else commit), work)
    source_id, repository = _local_identity(local, common)
    origin = Origin("repo", local.name, branch, commit, source_id, repository=repository, repo_prefix=prefix)
    return Source(work, work, origin)


def fetch_path(spec, work):
    """A local folder. In a Git repository only committed files install; otherwise the folder as it is."""
    local = Path(spec["path"]).resolve()
    if not local.is_dir():
        raise InstallError(f"{local.name} is not a folder")
    if git_location(local) is None:
        source_id = "local:" + hashlib.sha256(str(local).encode()).hexdigest()
        return Source(local, local, Origin("path", local.name, None, None, source_id))
    clone_dir, commit, branch, top, common = _snapshot(local, work)
    # Export the whole repository, so each layout's settings scope includes its full path in it.
    unpack(git(clone_dir, "archive", commit), work)
    source_id, repository = _local_identity(local, common)
    origin = Origin("path", local.name, branch, commit, source_id, repository=repository)
    return Source(work / local.relative_to(top), work, origin)


def _github_archive(repo, ref, work):
    check_ref(ref)
    env = git_env()
    commit = subprocess.check_output(
        ["gh", "api", f"repos/{repo}/commits/{ref}", "--jq", ".sha"], text=True, env=env
    ).strip()
    if not COMMIT.fullmatch(commit) or COMMIT.fullmatch(ref) and commit != ref:
        raise InstallError(f"{repo} at {ref} did not resolve to that commit")
    unpack(subprocess.check_output(["gh", "api", f"repos/{repo}/tarball/{commit}"], env=env), work, strip_root=True)
    return commit


def _git_archive(url, ref, work):
    """Fetch one commit of a Git URL without submodules or tags, then unpack its archive."""
    safe = safe_url(url)
    check_ref(ref)
    bare = work.parent / (work.name + ".git")
    env = git_env()
    subprocess.run(["git", "init", "-q", "--bare", "--template=", str(bare)], check=True, env=env, cwd="/")
    options = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.ssh.allow=always"]
    try:
        subprocess.run(
            ["git", *options, "-C", str(bare), "fetch", "-q", "--depth", "1", "--no-tags",
             "--no-recurse-submodules", url, ref],
            check=True,
            env=env,
            cwd="/",
        )
    except subprocess.CalledProcessError:
        raise InstallError(f"Could not fetch {safe} at {ref}. Check the URL, the ref and your Git access") from None
    commit = git(bare, "rev-parse", "--verify", "FETCH_HEAD^{commit}", text=True).strip()
    if not COMMIT.fullmatch(commit) or COMMIT.fullmatch(ref) and commit != ref:
        raise InstallError(f"{safe} at {ref} did not resolve to that commit")
    unpack(git(bare, "archive", commit), work)
    return commit


def fetch_git(spec, work):
    """A Git URL at a pinned ref. GitHub URLs use the GitHub CLI when it is installed."""
    url, ref = spec["git"], spec["ref"]
    safe = safe_url(url)
    repository = github_repo(url) if "@" not in url.split("://", 1)[-1].split("/", 1)[0] else ""
    if url.startswith("git@github.com:"):
        repository = github_repo(url)
    if repository and shutil.which("gh"):
        commit = _github_archive(repository, ref, work)
    else:
        commit = _git_archive(url, ref, work)
    source_id = "github:" + repository.lower() if repository else "git:" + safe.lower().removesuffix(".git")
    return Source(work, work, Origin("git", safe, ref, commit, source_id, repository=repository or None))


def fetch_github(spec, work):
    """The layouts repo from GitHub, at a pinned ref."""
    repo, ref = spec.get("repo", DEFAULT_REPO), spec.get("ref", "main")
    commit = _github_archive(repo, ref, work)
    return Source(work, work, Origin("repo", repo, ref, commit, "github:" + repo.lower(), repository=repo))


def fetch(spec, directory, cache):
    """Unpack one source once."""
    if spec.get("local_repo"):
        key, function = ("local", spec["local_repo"]), fetch_local_repo
    elif spec.get("path"):
        key, function = ("path", spec["path"]), fetch_path
    elif spec.get("git"):
        key, function = ("git", spec["git"], spec["ref"]), fetch_git
    else:
        key, function = ("github", spec.get("repo", DEFAULT_REPO), spec.get("ref", "main")), fetch_github
    if key not in cache:
        work = Path(directory).resolve() / f"source-{len(cache)}"
        work.mkdir()
        source = function(spec, work)
        catalog_dir = source.catalog_dir.resolve()
        if not catalog_dir.is_relative_to(source.archive_root.resolve()):
            raise InstallError(f"{source.origin.label} leaves its repository")
        cache[key] = Source(catalog_dir, source.archive_root.resolve(), source.origin)
    return cache[key]


# --- Catalogs --------------------------------------------------------------------------


def read_catalog(catalog_dir, label):
    """The catalog's default ID and its entries, checked against schema version 1."""
    path = catalog_dir / "catalog.json"
    if path.is_symlink() or not path.is_file():
        raise InstallError(f"{label} has no catalog.json")
    try:
        catalog = json.loads(path.read_bytes())
    except ValueError:
        raise InstallError(f"{label}: catalog.json is not valid JSON") from None
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise InstallError(f"{label}: catalog.json needs schema_version 1")
    entries = catalog.get("layouts")
    if not isinstance(entries, list) or not entries:
        raise InstallError(f"{label}: catalog.json lists no layouts")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or not APP_ID.fullmatch(entry["id"])
            or not isinstance(entry.get("path"), str)
            or not entry["path"]
            or not isinstance(entry.get("name", ""), str)
        ):
            raise InstallError(f"{label}: each catalog.json entry needs a layout ID and a path")
    ids = [entry["id"] for entry in entries]
    if len(ids) != len(set(ids)):
        raise InstallError(f"{label}: catalog.json lists a layout ID twice")
    default = catalog.get("default")
    if default is not None and default not in ids:
        raise InstallError(f"{label}: the catalog default {default!r} is not in its layout list")
    return default, entries


def locate(catalog_dir, relative, label, *, allow_root=False):
    """The layout folder at `relative`, which must stay inside the source with no symlinks."""
    if Path(relative).is_absolute():
        raise InstallError(f"{label}: the layout path {relative} must be relative")
    candidate = Path(os.path.normpath(catalog_dir / relative))
    package = candidate.resolve()
    if package != candidate or not package.is_relative_to(catalog_dir) or package == catalog_dir and not allow_root:
        raise InstallError(f"{label}: the layout path {relative} leaves its source folder")
    if not package.is_dir() or (package / "layout.json").is_symlink() or not (package / "layout.json").is_file():
        raise InstallError(f"{label}: {relative} has no layout.json")
    return package


def _app(source, relative, *, expected=None, name=None, allow_root=False):
    label = source.origin.label
    package = locate(source.catalog_dir, relative, label, allow_root=allow_root)
    try:
        manifest = read_manifest(package)
    except ValueError:
        app_id = expected or package.name
        raise InstallError(
            f"{label}: {relative}/layout.json isn't valid for this version of Art Frames. "
            f'Update Art Frames first, or add skip = ["{app_id}"].'
        ) from None
    if expected and manifest.id != expected:
        raise InstallError(f"{label}: catalog ID {expected} does not match the ID {manifest.id} in its layout.json")
    origin = replace(
        source.origin,
        app_path=package.relative_to(source.archive_root).as_posix(),
        catalog_name=name or manifest.name,
    )
    return manifest.id, package, origin


def collect(plan, directory, *, report=print):
    """Every layout to install as (package, Origin), the first layout first."""
    plan = validate_plan(plan)
    cache, items, owners = {}, [], {}

    def add(app_id, package, origin):
        if app_id in owners:
            raise InstallError(
                f'Two sources give the layout ID "{app_id}": {owners[app_id]} and {origin.label}. '
                f'Add skip = ["{app_id}"] to one of them.'
            )
        owners[app_id] = origin.label
        items.append((package, origin))

    def summary(origin, names):
        commit = origin.commit or "no commit (plain folder)"
        report(f"{origin.label} ({origin.kind}): {commit}. Layouts: {', '.join(names) or 'none'}", flush=True)

    def unused(skip, entries, label):
        for app_id in sorted(set(skip) - {entry["id"] for entry in entries}):
            report(f"Note: skip lists {app_id}, which is not in {label}. Nothing to skip.", flush=True)

    spec = plan["layouts_repo"]
    source = fetch(spec, directory, cache)
    label = source.origin.label
    if spec.get("app"):
        add(*_app(source, spec["app"], allow_root=True))
    else:
        default, entries = read_catalog(source.catalog_dir, label)
        skip = set(spec.get("skip", []))
        unused(skip, entries, label)
        first = spec.get("layout") or default or entries[0]["id"]
        if first in skip:
            if spec.get("layout"):
                raise InstallError(f"layout names {first}, which skip also lists. Remove it from one of them")
            first = next((entry["id"] for entry in entries if entry["id"] not in skip), None)
        by_id = {entry["id"]: entry for entry in entries}
        if first not in by_id:
            raise InstallError(f"{label} has no layout {first} to install" if first else
                               f"skip leaves no layout to install from {label}")
        for entry in [by_id[first], *(entry for entry in entries if entry["id"] not in skip | {first})]:
            add(*_app(source, entry["path"], expected=entry["id"], name=entry.get("name")))
    summary(source.origin, list(owners))
    for spec in plan.get("sources", []):
        source = fetch(spec, directory, cache)
        _, entries = read_catalog(source.catalog_dir, source.origin.label)
        skip = set(spec.get("skip", []))
        unused(skip, entries, source.origin.label)
        names = []
        for entry in entries:
            if entry["id"] not in skip:
                add(*_app(source, entry["path"], expected=entry["id"], name=entry.get("name")))
                names.append(entry["id"])
        summary(source.origin, names)
    return items


def source_apps(spec, directory, *, report=print):
    """Every layout of one [[sources]] entry, checked with the source rules, as (package, Origin)."""
    validate_plan({"layouts_repo": {"repo": DEFAULT_REPO}, "sources": [spec]})
    source = fetch(spec, directory, {})
    _, entries = read_catalog(source.catalog_dir, source.origin.label)
    skip = set(spec.get("skip", []))
    items = [_app(source, entry["path"], expected=entry["id"], name=entry.get("name"))[1:]
             for entry in entries if entry["id"] not in skip]
    report(f"{source.origin.label} ({source.origin.kind}): {source.origin.commit or 'no commit (plain folder)'}. "
           f"Layouts: {', '.join(read_manifest(package).id for package, _ in items) or 'none'}", flush=True)
    return items


def check_items(items):
    """The install checks that need no installation: layout count, IDs and each package's rules."""
    check_app_count(len(items))
    manifests = [read_manifest(package) for package, _ in items]
    if len({manifest.id for manifest in manifests}) != len(manifests):
        raise InstallError("Each layout needs its own ID")
    for (package, origin), manifest in zip(items, manifests, strict=True):
        check_package(package, origin, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "layout", nargs="?", help="Catalog ID of the first layout. Omitted uses the catalog default. "
        "Every other layout in the catalog installs too"
    )
    parser.add_argument("--local-repo", type=Path, help="Use this Git repository's committed HEAD")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--ref", default="main")
    parser.add_argument("--app", help="Install one committed layout directory without a catalog")
    parser.add_argument("--skip", action="append", default=[], metavar="ID", help="Leave this catalog layout out")
    parser.add_argument(
        "--plan",
        help='JSON {"layouts_repo": {...}, "sources": [...]}, as remote.py update builds from the deploy settings',
    )
    parser.add_argument("--check", action="store_true", help="Check the catalogs and layouts, then stop. Installs nothing")
    parser.add_argument(
        "--path",
        type=Path,
        help="With --check: check this folder as a [[sources]] entry. A plain folder or a Git repository",
    )
    parser.add_argument(
        "--first-only",
        action="store_true",
        help="Install only the first layout, with no extra layouts. For a local run without Docker. "
        "A browser Compose profile runs no layout",
    )
    parser.add_argument(
        "--transfer-only",
        action="store_true",
        help="With --remote, transfer source only; remote.py update handles preparation and activation",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Transfer source over the existing SSH session, then prepare through local sudo authentication",
    )
    args = parser.parse_args()
    if args.transfer_only and not args.remote:
        parser.error("--transfer-only requires --remote")
    if args.check and (args.remote or args.first_only):
        parser.error("--check installs nothing. Leave out --remote and --first-only")
    if args.first_only and args.remote:
        parser.error("--first-only is for a local run. With --remote it would remove the extra layouts on the NAS")
    if args.path and not args.check:
        parser.error("--path works only with --check")
    if args.path and (args.plan or args.local_repo or args.app or args.layout):
        parser.error("--path checks one source folder. Leave out the other layout selection options")
    if args.app and (args.layout or args.skip):
        parser.error("--app installs one layout without a catalog. Leave out the catalog ID and --skip")
    if args.plan:
        if args.layout or args.app or args.local_repo or args.skip:
            parser.error("--plan replaces the other layout selection options")
        plan = json.loads(args.plan)
    else:
        repo = {"local_repo": str(args.local_repo)} if args.local_repo else {"repo": args.repo, "ref": args.ref}
        for key in ("app", "layout", "skip"):
            if getattr(args, key):
                repo[key] = getattr(args, key)
        plan = {"layouts_repo": repo}
    with tempfile.TemporaryDirectory(prefix="dayframe-app-") as directory:
        if args.path:
            spec = {"path": str(args.path.resolve())} | ({"skip": args.skip} if args.skip else {})
            items = source_apps(spec, directory)
        else:
            items = collect(plan, directory)
        if args.check:
            check_items(items)
            print(f"All {len(items)} layouts are valid. Nothing was installed.")
            return
        if args.first_only:
            items = items[:1]
        if args.remote:
            install_remote(items, prepare=not args.transfer_only)
            return
        report_changes(installed_apps(runtime_paths(ROOT).sites), items)
        manifests = install_all(ROOT, items)
        names = ", ".join(item.name for item in manifests)
        print(f"Installed {names}. Configure each layout on the Layouts page after starting Art Frames.")
        print(
            "Review each layout's server code before running its image. Layout servers are trusted code; "
            "the renderer remains isolated."
        )


if __name__ == "__main__":
    try:
        main()
    except InstallError as error:
        raise SystemExit("Layout installation failed: " + str(error)) from None
    except (ValueError, KeyError, StopIteration, OSError, subprocess.CalledProcessError):
        raise SystemExit(
            "Layout installation failed. Check the repository, catalog ID and dedicated installation paths."
        ) from None

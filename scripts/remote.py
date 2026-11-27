"""Local SSH helper. Host details and the temporary connection stay out of Git."""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from art_frames import names  # noqa: E402
from scripts import app_sources  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MARKER = names.SOURCE_MARKER
PROJECT = names.COMPOSE_PROJECT
RUNTIME = ("data", "sites", ".secrets", ".env", "config.local.toml")
SERVICES = (names.CONTROLLER_SERVICE, "renderer", "website")
OLD_LAYOUTS, RENAMED_LAYOUTS = "dayframe-layouts", "art-frames-layouts"
ROLLED_BACK = 3  # update-remote.sh: new version failed; previous images restored and healthy


def target(config):
    host, user = config["host"], config["user"]
    path = config["path"]
    port = config.get("port", 22)
    compose = config.get("compose", "compose.yaml")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("Use a plain IPv4 address or hostname")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", user):
        raise ValueError("Invalid SSH username")
    parts = PurePosixPath(path).parts
    if (
        not re.fullmatch(r"/[A-Za-z0-9_./-]+/(dayframe|art-frames)", path)
        or ".." in parts
        or str(PurePosixPath(path)) != path
        or len(parts) < 4
    ):
        raise ValueError("Use a dedicated absolute path ending in /dayframe (or /art-frames for a new install), "
            "without dot components")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid SSH port")
    if compose not in (
        "compose.yaml",
        "compose.host.yaml",
        "compose.static.yaml",
        "compose.static.host.yaml",
    ):
        raise ValueError("Choose a shipped browser or static Compose file")
    if not re.fullmatch(
        r"(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)+|[A-Za-z0-9_][A-Za-z0-9_.-]*", config.get("rsync_path", "rsync")
    ):
        raise ValueError("rsync_path must be an executable name or absolute path without arguments")
    return f"{user}@{host}", path, port, compose


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def ssh_options(config):
    remote, _, port, _ = target(config)
    local = ROOT / ".deploy"
    if local.is_symlink():
        raise ValueError("Refusing a symlinked local connection directory")
    local.mkdir(mode=0o700, exist_ok=True)
    local.chmod(0o700)
    identity = hashlib.sha256(f"{remote}:{port}".encode()).hexdigest()[:12]
    socket = local / f"ssh-{identity}"
    if len(os.fsencode(socket)) > 100:
        raise ValueError("Repository path is too long for an SSH socket")
    return [
        "ssh",
        "-p",
        str(port),
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ConnectTimeout=8",
        "-S",
        str(socket),
    ]


def sync_guard(path, *, initialize=True):
    # Only the source child is mirrored. Runtime data never lives in that child.
    return f"""set -eu
umask 077
base={shlex.quote(path)}
test -d "$(dirname "$base")"
test "$(cd "$(dirname "$base")" && pwd -P)" = "$(dirname "$base")"
test ! -L "$base"
{'mkdir -p "$base"' if initialize else 'test -d "$base"'}
test ! -L "$base/.dayframe-managed"
if test -e "$base/source" || test -L "$base/source"; then
    test ! -L "$base/source"
    test -f "$base/.dayframe-managed"
    test "$(cat "$base/.dayframe-managed")" = {MARKER}
else
    {"true" if initialize else "echo 'Run sync once to initialize the managed source directory' >&2; exit 1"}
    test -z "$(ls -A "$base")" || {{ echo 'Target is not an empty Art Frames directory' >&2; exit 1; }}
    printf '%s\\n' {MARKER} > "$base/.dayframe-managed"
    mkdir "$base/source"
fi
command -v rsync >/dev/null
"""


def require_clean_checkout(root):
    dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, capture_output=True)
    if dirty.stdout:
        raise ValueError("Commit tracked changes before syncing; only committed source is deployed")


def sync(config, ssh, *, dry_run=False):
    remote, path, _, _ = target(config)
    require_clean_checkout(ROOT)
    with tempfile.TemporaryDirectory(prefix="dayframe-source-") as directory:
        archive = Path(directory) / "source.tar"
        with archive.open("wb") as output:
            run(["git", "archive", "HEAD"], cwd=ROOT, stdout=output)
        source = Path(directory) / "source"
        source.mkdir()
        with tarfile.open(archive) as files:
            files.extractall(source, filter="data")
        if any((source / item).exists() or (source / item).is_symlink() for item in RUNTIME):
            raise ValueError("Committed source contains a reserved runtime path")
        # BatchMode never prompts for credentials in an agent or pipe. Connect locally first.
        run([*ssh, "-o", "BatchMode=yes", remote, sync_guard(path, initialize=not dry_run)])
        args = [
            "rsync",
            "-rlpt",
            "--delete",
            "--itemize-changes",
            "--rsync-path=" + config.get("rsync_path", "rsync"),
        ]
        if dry_run:
            args.append("--dry-run")
        for item in RUNTIME:
            args += ["--exclude", "/" + item]
        run(
            [
                *args,
                "-e",
                shlex.join([*ssh, "-o", "BatchMode=yes"]),
                str(source) + "/",
                remote + ":" + path + "/source/",
            ]
        )


def source_committed(path, label):
    """Stop when a local layout source has uncommitted changes. Git never runs in that repository."""
    with tempfile.TemporaryDirectory(prefix="dayframe-check-") as work:
        app_sources.require_committed(path, label, work)


def app_plan(config):
    """The installer plan from .deploy.local.toml, or None to keep the installed layouts.

    [app] names the layouts repo. Every layout in its catalog.json installs; the catalog
    default (or layout) is the first layout. Each [[sources]] entry adds every layout in another
    catalog. skip leaves layouts out. Every rule is checked here, before any connection.
    """
    if "apps" in config:
        raise ValueError(
            "[[apps]] is no longer used. Layouts in the layouts repo install automatically. "
            "To add layouts from another place, use [[sources]]."
        )
    app, sources = config.get("app"), config.get("sources")
    if app is None:
        if sources is not None:
            raise ValueError("[[sources]] adds layouts to the layouts repo. Add an [app] table that names it.")
        return None
    if isinstance(app, dict) and "layouts" in app:
        raise ValueError(
            "layouts = [...] is no longer used. Every layout in the catalog installs; use skip = [...] to leave some out."
        )
    if not isinstance(app, dict) or not app:
        raise ValueError("Use an [app] table with local_repo, or repo and ref, plus optional layout and skip")
    if not isinstance(sources, (list, type(None))) or not all(isinstance(item, dict) for item in sources or []):
        raise ValueError("Write each extra source as a [[sources]] table")
    plan = {"layouts_repo": dict(app)}
    if sources is not None:
        plan["sources"] = [dict(source) for source in sources]
    app_sources.validate_plan(plan)
    repo = plan["layouts_repo"]
    if "local_repo" in repo:
        folder = (ROOT / Path(repo["local_repo"]).expanduser()).resolve()
        renamed = folder.with_name(RENAMED_LAYOUTS)
        if not folder.is_dir() and folder.name == OLD_LAYOUTS and renamed.is_dir():
            # The layouts folder was renamed with the product; .deploy.local.toml may still name the old one.
            print(f"Using ../{RENAMED_LAYOUTS} (local_repo still says {repo['local_repo']}).", flush=True)
            folder = renamed
        if not folder.is_dir():
            raise ValueError(f"The layouts repo folder {repo['local_repo']} does not exist")
        repo["local_repo"] = str(folder)
    for source in plan.get("sources", []):
        if "path" in source:
            folder = (ROOT / Path(source["path"]).expanduser()).resolve()
            if not folder.is_dir():
                raise ValueError(f"The source folder {source['path']} does not exist")
            source["path"] = str(folder)
    if "local_repo" in repo:
        source_committed(Path(repo["local_repo"]), Path(repo["local_repo"]).name)
    for source in plan.get("sources", []):
        if "path" in source:
            source_committed(Path(source["path"]), Path(source["path"]).name)
    return plan


def app_update_args(config):
    """The installer command that transfers every configured layout, or None to keep the installed layouts."""
    plan = app_plan(config)
    if plan is None:
        return None
    return [
        sys.executable,
        str(ROOT / "scripts/install-layout.py"),
        "--remote",
        "--transfer-only",
        "--plan",
        json.dumps(plan),
    ]


# How long the shared SSH connection, and the app tunnel on it, stays open when idle.
CONTROL_PERSIST = "12h"


def ensure_connection(config, ssh):
    remote, _, _, _ = target(config)
    try:
        run([*ssh, "-O", "check", remote], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        # A stale socket would make the new master fall back to a plain connection,
        # and every later BatchMode command would then fail or prompt again.
        if "-S" in ssh:
            Path(ssh[ssh.index("-S") + 1]).unlink(missing_ok=True)
        # The only interactive SSH login; every later command reuses this master.
        run([*ssh, "-M", "-N", "-f", "-o", f"ControlPersist={CONTROL_PERSIST}", remote])


def open_tunnel(config, ssh):
    remote, _, _, _ = target(config)
    forward = ["-L", "127.0.0.1:8765:127.0.0.1:8765", remote]
    try:
        run([*ssh, "-O", "forward", *forward], capture_output=True)
    except subprocess.CalledProcessError:
        # Replace only this master's exact app forward, never another listener.
        try:
            run([*ssh, "-O", "cancel", *forward], capture_output=True)
        except subprocess.CalledProcessError:
            pass
        run([*ssh, "-O", "forward", *forward])


# The new Keychain service first, then the one saved before the rename. Reading the old one
# avoids a second sudo prompt; save_password writes to the one that exists.
KEYCHAIN_SERVICES = ("art-frames-management", "dayframe-management")
KEYCHAIN_ACCOUNT = names.LOGIN_USER


def keychain_find(service, *extra):
    try:
        found = subprocess.run(["security", "find-generic-password", "-s", service, "-a", KEYCHAIN_ACCOUNT, *extra],
                               capture_output=True, text=True)
    except OSError:
        return None
    return found if found.returncode == 0 else None


def keychain_password():
    for service in KEYCHAIN_SERVICES:
        found = keychain_find(service, "-w")
        if found and found.stdout.strip():
            return found.stdout.strip()
    return None


def keychain_service():
    """The service that holds the saved password, else the new one."""
    return next((service for service in KEYCHAIN_SERVICES if keychain_find(service)), KEYCHAIN_SERVICES[0])


def save_password(config, ssh):
    """Read the generated password once with sudo and keep it only in the macOS Keychain."""
    remote, path, _, _ = target(config)
    inner = "printf DAYFRAME-PW:; cat " + shlex.quote(path + "/.secrets/web_password") + "; echo"
    print("Saving the management password to your Keychain. Enter the NAS administrator password:", flush=True)
    # The sudo prompt is silent; the password reaches this process, never the terminal.
    result = subprocess.run(
        [*ssh, "-o", "BatchMode=yes", "-tt", remote, "sudo -p '' sh -c " + shlex.quote(inner)],
        stdout=subprocess.PIPE,
        text=True,
    )
    print(flush=True)
    found = re.search(r"DAYFRAME-PW:([A-Za-z0-9+/=]{16,})", result.stdout)
    if result.returncode or not found:
        raise ValueError("Could not read the management password from the NAS")
    # security -i reads the command from stdin, so the password never appears in a process list.
    service = keychain_service()
    command = f"add-generic-password -U -s {service} -a {KEYCHAIN_ACCOUNT} -l 'Art Frames' -w {found.group(1)}\n"
    run(["security", "-i"], input=command, text=True, capture_output=True)
    return found.group(1)


def home_network_url(config):
    """The app's address on the home network when it answers there, else None.

    Only the health page is asked, without a password, with a short timeout.
    """
    url = f"http://{config['host']}:8765/"
    try:
        with urllib.request.urlopen(url + "health", timeout=2) as response:
            return url if response.status == 200 else None
    except (OSError, ValueError):
        return None


def print_home_network_hint(config):
    url = home_network_url(config)
    if url:
        print("On your home network: " + url, flush=True)
    else:
        print("To open it from phones and computers at home, run `npm run lan`.", flush=True)


def open_management(config, ssh, refresh=False):
    ensure_connection(config, ssh)
    open_tunnel(config, ssh)
    url = "http://127.0.0.1:8765/"
    if sys.platform == "darwin":
        password = (None if refresh else keychain_password()) or save_password(config, ssh)
        run(["pbcopy"], input=password, text=True)
        print("Password copied to the clipboard. Paste it in the sign-in form.", flush=True)
        run(["open", url])
    print("Art Frames management: " + url, flush=True)
    print_home_network_hint(config)


def clear_incoming_command(path):
    """Remove sites/.incoming, the layouts from a transfer that was not placed."""
    sites = shlex.quote(path + "/sites")
    return f"test ! -L {sites} && rm -rf -- {sites}/.incoming"


def update(config, ssh):
    remote, path, _, compose = target(config)
    require_clean_checkout(ROOT)
    app_args = app_update_args(config)
    if app_args and compose_mode(compose) == "browser":
        # The browser profile runs no layouts; prepare-remote.sh removes their settings.
        print(app_sources.BROWSER_NOTE, flush=True)
        app_args = None
    ensure_connection(config, ssh)
    print("Syncing committed Art Frames source…", flush=True)
    sync(config, ssh)
    if app_args:
        print("Syncing the layouts…", flush=True)
        try:
            run(app_args, cwd=ROOT)
        except subprocess.CalledProcessError:
            raise ValueError("Installing the layouts failed. Read the message above. Nothing was activated.") from None
    else:
        # No layouts go this time. Layouts sent by an earlier update that stopped must not move
        # into place now.
        run([*ssh, "-o", "BatchMode=yes", remote, clear_incoming_command(path)])
    print(
        "Preparing, building and checking Art Frames. Enter the NAS administrator password if prompted.",
        flush=True,
    )
    command = "sudo sh " + shlex.quote(path + "/source/scripts/update-remote.sh") + " " + shlex.quote(compose)
    try:
        run([*ssh, "-o", "BatchMode=yes", "-t", remote, command])
    except subprocess.CalledProcessError as error:
        if error.returncode == ROLLED_BACK:
            open_tunnel(config, ssh)
            raise ValueError(
                "Update failed. Art Frames rolled back to the previous images and is running. "
                "Run `npm run logs` to see why the new version failed."
            ) from None
        raise ValueError("Update failed. Read the output above, then run `npm run logs` for container errors.") from None
    open_tunnel(config, ssh)
    print("Art Frames updated and healthy. Open http://127.0.0.1:8765/", flush=True)
    print_home_network_hint(config)


def rollback(config, ssh):
    remote, path, _, compose = target(config)
    ensure_connection(config, ssh)
    print("Restarting the previous images. Enter the NAS administrator password if prompted.", flush=True)
    command = "sudo sh " + shlex.quote(path + "/source/scripts/rollback-remote.sh") + " " + shlex.quote(compose)
    run([*ssh, "-o", "BatchMode=yes", "-t", remote, command])
    open_tunnel(config, ssh)
    print("Art Frames rolled back and healthy. Open http://127.0.0.1:8765/", flush=True)


def compose_mode(compose):
    """static or browser, from the same helper as the admin scripts."""
    return subprocess.run(
        ["sh", "-c", '. "$0" && compose_mode "$1"', str(Path(__file__).resolve().parent / "compose-files.sh"), compose],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def compose_files(compose):
    """Shell text for the -f options of a validated profile. Run it from the source folder.

    It calls the same helper as the admin scripts, scripts/compose-files.sh, which adds the
    generated override for extra layouts on a static profile when the override exists.
    """
    return f"$(. scripts/compose-files.sh && compose_files {compose})"


def logs_command(config, service=None, *, follow=False, since=None):
    """since: a period such as 24h or 90m; it shows every line from then instead of the last 200."""
    _, path, _, compose = target(config)
    if since is not None and not re.fullmatch(r"[1-9][0-9]{0,3}[mh]", since):
        raise ValueError("Give --since as minutes or hours, for example 90m or 24h")
    if service is not None and service not in SERVICES and not re.fullmatch(r"app-[a-z][a-z0-9-]{0,63}", service):
        raise ValueError("Choose a service: " + ", ".join(SERVICES) + " or app-<id> for an extra layout")
    name = " " + service if service else ""
    first = ""
    if service and service.startswith("app-"):
        # The first layout runs as the website service, not as app-<id>. Its ID is the first
        # field of sites/installed/.install-profile, which the SSH user can read without sudo.
        # The service name is checked above, so it is safe in the shell text.
        first = (
            f"service={service}; first=$(cut -d' ' -f1 sites/installed/.install-profile 2>/dev/null || true); "
            'if test "app-$first" = "$service"; then '
            f'echo "{service[4:]} is the first layout. It runs as the website service." >&2; service=website; fi; '
        )
        name = ' "$service"'
    return (
        "cd "
        + shlex.quote(path + "/source")
        + f' && {first}sudo "$(command -v docker)" compose -p {PROJECT} {compose_files(compose)} logs '
        + (f"--since {since}" if since else "--tail 200")
        + (" --follow" if follow else "")
        + name
    )


def logs(config, ssh, service=None, *, follow=False, since=None):
    remote, _, _, _ = target(config)
    command = logs_command(config, service, follow=follow, since=since)
    ensure_connection(config, ssh)
    run([*ssh, "-o", "BatchMode=yes", "-t", remote, 'PATH="$PATH:/usr/local/bin"; export PATH; ' + command])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "connect",
            "update",
            "rollback",
            "logs",
            "open",
            "disconnect",
            "check",
            "plan",
            "sync",
            "prepare",
            "up",
            "status",
            "tunnel",
            "enable-tunnel",
            "rename-check",
            "lan",
            "lan-off",
        ),
    )
    parser.add_argument("service", nargs="?", help="logs only: dayframe, renderer, website or app-<id>")
    parser.add_argument("-f", "--follow", action="store_true", help="logs only: keep streaming")
    parser.add_argument("--since", help="logs only: every line from this long ago, for example 24h or 90m")
    parser.add_argument("--new-password", action="store_true", help="open only: read the password from the NAS again")
    parser.add_argument("--dry-run", action="store_true", help="rename-check only: checks and plan, no backup")
    args = parser.parse_args()
    if args.action != "logs" and (args.service or args.follow or args.since is not None):
        parser.error("Only logs accepts a service, --follow or --since")
    if args.action != "open" and args.new_password:
        parser.error("Only open accepts --new-password")
    if args.action != "rename-check" and args.dry_run:
        parser.error("Only rename-check accepts --dry-run")
    config = tomllib.loads((ROOT / ".deploy.local.toml").read_text())
    remote, path, _, compose = target(config)
    ssh = ssh_options(config)
    if args.action == "update":
        update(config, ssh)
    elif args.action == "rollback":
        rollback(config, ssh)
    elif args.action == "logs":
        try:
            logs(config, ssh, args.service, follow=args.follow, since=args.since)
        except ValueError as error:
            parser.error(str(error))
    elif args.action == "open":
        open_management(config, ssh, refresh=args.new_password)
    elif args.action == "connect":
        run([*ssh, "-M", "-N", "-f", "-o", f"ControlPersist={CONTROL_PERSIST}", remote])
        print("Temporary SSH connection ready; stays open for 12 idle hours. No key was installed.")
    elif args.action == "disconnect":
        run([*ssh, "-O", "exit", remote])
    elif args.action in ("sync", "plan"):
        sync(config, ssh, dry_run=args.action == "plan")
    elif args.action == "tunnel":
        open_tunnel(config, ssh)
    else:
        mode = compose_mode(compose)
        up_command = (
            "cd "
            + shlex.quote(path + "/source")
            + " && sudo sh -c "
            + shlex.quote(
                'set -eu; PATH="$PATH:/usr/local/bin"; export PATH; . scripts/compose-files.sh; '
                f"files=$(compose_files {compose}); "
                f"sh scripts/build-check.sh {compose}; "
                # --remove-orphans stops the container of a layout that is no longer installed.
                f"docker compose -p {PROJECT} $files up -d --remove-orphans"
            )
        )
        lan_script = shlex.quote(path + "/source/scripts/lan-access.sh")
        # The NAS gets the script with the update. Without it, say so instead of a shell error.
        def lan_command(state):
            return (
                f"test -f {lan_script} || {{ echo 'Run npm run deploy first.' >&2; exit 1; }}; "
                f"sudo sh {lan_script} {shlex.quote(path)} {state} {shlex.quote(config['host'])} && " + up_command
            )

        commands = {
            "check": f"set -eu; id; uname -m; command -v rsync; command -v docker; command -v openssl; command -v sudo; test -d {shlex.quote(str(PurePosixPath(path).parent))}; echo 'Target parent exists'",
            "prepare": f"sudo sh {shlex.quote(path + '/source/scripts/prepare-remote.sh')} {mode}",
            "enable-tunnel": f"sudo sh {shlex.quote(path + '/source/scripts/enable-tunnel.sh')} {shlex.quote(config['user'])}",
            # Run sync first: it reads the synced source. It moves and deletes nothing.
            "rename-check": f"sudo sh {shlex.quote(path + '/source/scripts/rename-check.sh')}"
            + (" --dry-run" if args.dry_run else "") + f" {compose}",
            "up": up_command,
            "lan": lan_command("on"),
            "lan-off": lan_command("off"),
            "status": f'cd {shlex.quote(path + "/source")} && sudo "$(command -v docker)" compose -p {PROJECT} '
            f"{compose_files(compose)} ps",
        }
        if args.action in ("lan", "lan-off"):
            ensure_connection(config, ssh)
        run(
            [
                *ssh,
                "-o",
                "BatchMode=yes",
                *(["-t"] if args.action != "check" else []),
                remote,
                'PATH="$PATH:/usr/local/bin"; export PATH; ' + commands[args.action],
            ]
        )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None

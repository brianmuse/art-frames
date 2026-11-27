"""The layout plan and safe access to layout sources. Shared by remote.py and install-layout.py.

A plan names the layouts repo and any extra sources:

    {"layouts_repo": {"local_repo": "/abs/path"} | {"repo": "owner/name", "ref": "main"},
                     plus optional "layout" (the first layout) or "app" (one layout, no catalog),
                     plus optional "skip": ["id", ...],
     "sources": [{"path": "/abs/path"} | {"git": "https://...", "ref": "v1"},
                 plus optional "skip": [...]]}

Git commands never run inside a local source repository. That repository's own
.git/config could name a command (core.fsmonitor, a smudge filter) that would then run
on this computer. A bare clone with its own configuration is read instead.
"""

import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

APP_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
OWNER_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}")
COMMIT = re.compile(r"[0-9a-f]{40}")
DEFAULT_REPO = "brianmuse/art-frames-layouts"
LAYOUTS_REPO_KEYS = {"layout", "local_repo", "repo", "ref", "app", "skip"}
SOURCE_KEYS = {"path", "git", "ref", "skip"}
GIT_URL_HELP = "Use an https://, ssh:// or user@host:path Git URL"
BROWSER_NOTE = (
    "A browser Compose profile runs no layout. "
    'Set compose = "compose.static.yaml" in .deploy.local.toml to run them.'
)
# A scp-style address, such as git@github.com:owner/apps.git.
SCP = re.compile(r"([A-Za-z0-9_.-]+@)?([A-Za-z0-9][A-Za-z0-9.-]*):([A-Za-z0-9_][A-Za-z0-9_./~-]*)")


class InstallError(ValueError):
    """A problem the person can fix. The message is shown as is."""


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 1000


def skip_ids(table, where):
    skip = table.get("skip", [])
    if (
        not isinstance(skip, list)
        or any(not isinstance(item, str) or not APP_ID.fullmatch(item) for item in skip)
        or len(set(skip)) != len(skip)
    ):
        raise InstallError(f'skip in {where} must list different layout IDs, such as skip = ["news"]')
    return skip


def check_ref(ref):
    if not isinstance(ref, str) or not REF.fullmatch(ref) or ".." in ref:
        raise InstallError(f"Use a plain Git branch, tag or commit as ref, not {ref!r}")
    return ref


def safe_url(url):
    """The Git URL without user name, password or token. Allows only https, ssh and user@host:path."""
    scp = SCP.fullmatch(url) if isinstance(url, str) and "://" not in url else None
    if scp:
        return f"{scp.group(2)}:{scp.group(3)}"
    parsed = urlsplit(url if isinstance(url, str) else "")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if (
        parsed.scheme not in ("https", "ssh")
        or not parsed.hostname
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", parsed.hostname)
        or not re.fullmatch(r"/[A-Za-z0-9_./~-]+", parsed.path or "/")
        or parsed.query
        or parsed.fragment
    ):
        raise InstallError(GIT_URL_HELP + " for a git source")
    return f"{parsed.scheme}://{parsed.hostname}{f':{port}' if port else ''}{parsed.path}"


def github_repo(address):
    """owner/repo for a GitHub address, or ""."""
    if address.startswith("git@github.com:"):
        repository = address.split(":", 1)[1]
    else:
        parsed = urlsplit(address)
        repository = parsed.path.lstrip("/") if parsed.hostname == "github.com" else ""
    repository = repository.removesuffix(".git")
    return repository if OWNER_REPO.fullmatch(repository) else ""


def validate_plan(plan):
    """Check the whole plan. Every rule lives here, so remote.py can check before it connects."""
    if (
        not isinstance(plan, dict)
        or set(plan) - {"layouts_repo", "sources"}
        or not isinstance(plan.get("layouts_repo"), dict)
    ):
        raise InstallError("The layout plan needs a layouts_repo")
    repo, sources = plan["layouts_repo"], plan.get("sources", [])
    if set(repo) - LAYOUTS_REPO_KEYS or any(not _text(value) for key, value in repo.items() if key != "skip"):
        raise InstallError("The [app] settings must be nonempty text: local_repo, or repo and ref, plus layout or skip")
    if "app" in repo and ("layout" in repo or "skip" in repo):
        raise InstallError('app = "..." installs one layout without a catalog. Leave out layout and skip')
    if "layout" in repo and not APP_ID.fullmatch(repo["layout"]):
        raise InstallError(f"layout must be a layout ID, not {repo['layout']!r}")
    if "local_repo" in repo and ("ref" in repo or "repo" in repo):
        raise InstallError("A local_repo installs its committed HEAD. Leave out ref and repo.")
    if "local_repo" not in repo:
        if not OWNER_REPO.fullmatch(repo.get("repo", DEFAULT_REPO)):
            raise InstallError("repo must be owner/name, such as brianmuse/art-frames-layouts")
        check_ref(repo.get("ref", "main"))
    skip_ids(repo, "[app]")
    if not isinstance(sources, list):
        raise InstallError("Write each extra source as a [[sources]] table")
    for source in sources:
        if (
            not isinstance(source, dict)
            or set(source) - SOURCE_KEYS
            or ("path" in source) == ("git" in source)
            or any(not _text(value) for key, value in source.items() if key != "skip")
        ):
            raise InstallError('Each [[sources]] entry needs path = "..." or git = "...", plus optional ref and skip')
        if "git" in source:
            safe_url(source["git"])
            if "ref" not in source:
                raise InstallError(f'The git source {safe_url(source["git"])} needs a ref, such as ref = "v1.2.0"')
            check_ref(source["ref"])
        elif "ref" in source:
            raise InstallError("A path source installs its folder as it is now. Leave out ref")
        skip_ids(source, "[[sources]]")
    return plan


# --- Local Git repositories, read through a bare clone -----------------------------------


# Variables that would point Git at another repository than the one named.
REPOSITORY_VARIABLES = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
}


CONFIG_VARIABLES = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def git_env(*, isolated=False):
    """The environment for Git, without repository variables or GH_TOKEN.

    isolated=True also removes every configuration that does not come from the repository
    Git runs in: the user's and the system's config files, and settings passed in the
    environment (GIT_CONFIG_PARAMETERS, GIT_CONFIG_COUNT, GIT_CONFIG_KEY_<n> and
    GIT_CONFIG_VALUE_<n>). Git then runs only what that repository's own config defines.
    """
    env = {name: value for name, value in os.environ.items() if name not in REPOSITORY_VARIABLES | {"GH_TOKEN"}}
    env["GIT_TERMINAL_PROMPT"] = "0"
    if isolated:
        env = {name: value for name, value in env.items() if not name.startswith(CONFIG_VARIABLES)}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def git_location(path):
    """(work tree top, Git common directory) of the repository that holds `path`, or None.

    Found by reading the file system, not by running Git in that repository.
    """
    path = Path(path).resolve()
    for parent in (path, *path.parents):
        dotgit = parent / ".git"
        if dotgit.is_dir():
            return parent, dotgit
        if dotgit.is_file():
            text = dotgit.read_text().strip()
            if not text.startswith("gitdir:"):
                return None
            gitdir = (parent / text[len("gitdir:"):].strip()).resolve()
            common = gitdir / "commondir"
            return parent, ((gitdir / common.read_text().strip()).resolve() if common.is_file() else gitdir)
    return None


def _config(*args):
    result = subprocess.run(["git", "config", *args], capture_output=True, text=True, cwd="/", env=git_env())
    return result.stdout


def origin_url(common):
    """remote.origin.url as `git remote get-url origin` gives it, without running Git in the repository.

    The repository's config file is read as a plain file. url.<base>.insteadOf rules from
    that file and from the user's and the system's configuration apply, longest match
    first, as Git applies them. The settings scope of a layout depends on this URL.
    """
    repository = ["--file", str(common / "config"), "--no-includes"]
    url = _config(*repository, "--get", "remote.origin.url").strip()
    best, base = "", ""
    for scope in (repository, ["--global"], ["--system"]):
        for line in _config(*scope, "--get-regexp", r"^url\..*\.insteadof$").splitlines():
            key, _, prefix = line.partition(" ")
            if prefix and url.startswith(prefix) and len(prefix) > len(best):
                best, base = prefix, key[len("url.") : -len(".insteadof")]
    return base + url[len(best) :] if best else url


def clone(top, destination):
    """A bare clone of the repository at `top`, with only Git's own settings: no template,
    and the user's, the system's and the environment's configuration off (isolated).
    A bare clone checks nothing out, so no filter or checkout hook runs."""
    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "clone",
            "-q",
            "--bare",
            "--no-local",
            "--template=",
            "--",
            str(top),
            str(destination),
        ],
        check=True,
        capture_output=True,
        cwd="/",
        env=git_env(isolated=True),
    )
    return destination


def git(clone_dir, *args, **kwargs):
    """Git in a clone, with the user's and the system's configuration turned off."""
    return subprocess.check_output(
        ["git", "--git-dir", str(clone_dir), *args], env=git_env(isolated=True), cwd="/", **kwargs
    )


def tracked_changes(top, clone_dir):
    """Tracked files whose content differs from the committed HEAD, as {name: filter or None}.

    Git never runs in the work tree. The clone hashes each file with the committed
    attributes (--attr-source=HEAD), so text and eol conversion apply as Git applies them.
    No filter runs: the clone's configuration defines none, and the user's is off. A file
    with a filter attribute (such as Git LFS) therefore can't be checked; its filter is named.
    """
    listing = git(clone_dir, "ls-tree", "-r", "-z", "HEAD").split(b"\0")
    files = {}
    for line in filter(None, listing):
        meta, _, name = line.partition(b"\t")
        mode, kind, blob = meta.split()
        if kind == b"blob" and mode in (b"100644", b"100755"):
            files[os.fsdecode(name)] = blob.decode()
    changed, present = [], []
    for name in files:
        path = top / name
        if path.is_symlink() or not path.is_file():
            changed.append(name)
        else:
            present.append(name)
    if present:
        # Most files match byte for byte. Only the others need a conversion check, one each.
        hashes = git(
            clone_dir,
            "hash-object",
            "--no-filters",
            "--stdin-paths",
            input="\n".join(str(top / name) for name in present).encode(),
        ).split()
        for name, digest in zip(present, hashes, strict=True):
            if digest.decode() != files[name]:
                converted = git(clone_dir, "--attr-source=HEAD", "hash-object", "--path=" + name, str(top / name))
                if converted.decode().strip() != files[name]:
                    changed.append(name)
    result = {}
    if changed:
        attributes = git(clone_dir, "--attr-source=HEAD", "check-attr", "-z", "filter", "--", *changed).split(b"\0")
        filters = {
            os.fsdecode(attributes[index]): attributes[index + 2].decode()
            for index in range(0, len(attributes) - 2, 3)
        }
        for name in sorted(changed):
            value = filters.get(name)
            result[name] = value if value not in (None, "unspecified", "unset") else None
    return result


def require_committed(path, label, work):
    """Stop when the Git repository that holds `path` has uncommitted changes to tracked files."""
    location = git_location(path)
    if location is None:
        return
    changed = tracked_changes(location[0], clone(location[0], Path(work) / "check.git"))
    filtered = [f"{name} uses a Git filter ({value}); Art Frames can't check it" for name, value in changed.items() if value]
    plain = [name for name, value in changed.items() if not value]
    if filtered:
        raise InstallError(f"{label}: " + ". ".join(filtered[:5]) + ". Remove the filter attribute, or skip this source")
    if plain:
        shown = ", ".join(plain[:5]) + (" and more" if len(plain) > 5 else "")
        raise InstallError(f"{label} has uncommitted changes ({shown}). Commit them first: deploy installs only committed files")

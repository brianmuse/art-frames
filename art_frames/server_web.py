"""The Server page (/server): the address for other devices, the password and sign-in, updates and backups.

These settings belong to the Art Frames server, not to a TV, so the page is the same for every TV.
"""

from __future__ import annotations

import io
from datetime import datetime
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import apps, auth, backup, qr

pages = Blueprint("server", __name__)
EXPORT_LIMIT = 25_000_000


def _ext(name):
    return current_app.extensions[name]


def address_view():
    """The addresses for other devices, the address this browser uses now, and why a list may be empty."""
    reach = _ext("reach")
    here = request.host_url.rstrip("/")
    urls = reach.urls()
    host = urlsplit(here).hostname or ""
    return {
        "here": here,
        "host": host,
        "loopback": host in ("localhost", "127.0.0.1", "::1"),
        "urls": urls,
        # A phone camera opens the number address more reliably than name.local (as in Reach.banner).
        "qr": next((url for url in urls if ".local" not in url), urls[0] if urls else here),
        "local_only": reach.local_only,
        "bridge": reach.bridge,
        "mdns": f"{reach.mdns_name}.local" if reach.mdns_name else "",
    }


def page(errors=None, code=200):
    signin = _ext("auth")
    return render_template(
        "server.html", address=address_view(), password_mode=signin.mode, minimum=auth.MINIMUM,
        updates=_ext("updates").view(), errors=errors or {},
    ), code


@pages.get("/server")
def server_page():
    return page()


@pages.get("/server/address.svg")
def address_qr():
    """A QR code of the first address on the page, for a phone to scan."""
    body = qr.svg(address_view()["qr"], title="Art Frames address")
    return current_app.response_class(body, mimetype="image/svg+xml")


@pages.post("/server/password")
def change_password():
    signin = _ext("auth")
    try:
        signin.set_password(request.form.get("password", ""), request.form.get("repeat", ""),
                            current=request.form.get("current", ""))
    except auth.PasswordError as error:
        return page({"password": str(error)}, 400)
    # The new password signs out every other device. This one stays signed in.
    session.permanent = True
    session["auth"] = signin.mark()
    flash("Password changed. Other devices must sign in again.", "card-password:success")
    return redirect(url_for("server.server_page", _anchor="password"))


@pages.post("/server/updates")
def save_updates():
    updates = _ext("updates")
    try:
        updates.set_enabled(request.form.get("check") == "on")
    except ValueError as error:
        return page({"updates": str(error)}, 400)
    flash("Saved.", "card-updates:success")
    return redirect(url_for("server.server_page", _anchor="updates"))


@pages.post("/server/updates/check")
def check_updates():
    updates = _ext("updates")
    if not updates.enabled:
        return page({"updates": "Turn on the update check first."}, 400)
    if updates.check():
        view = updates.view()
        if not view["latest"]:
            message = "There is no Art Frames release yet. You have the newest version."
        elif view["available"]:
            message = f"Art Frames {view['latest'].removeprefix('v')} is out."
        else:
            message = "You have the newest version."
        flash(message, "card-updates:success")
    else:
        flash("Art Frames could not reach GitHub. It tries again tomorrow.", "card-updates:error")
    return redirect(url_for("server.server_page", _anchor="updates"))


@pages.post("/server/export")
def export_settings():
    private = request.form.get("private") == "on"
    displays = _ext("displays")
    data = backup.export(displays.configs.root, displays.configs, apps.usable(displays.configs.load()), private=private)
    stamp = datetime.now().strftime("%Y-%m-%d")
    name = f"art-frames-backup-{stamp}{'-private' if private else ''}.zip"
    return send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True, download_name=name)


@pages.post("/server/import")
def import_settings():
    upload = request.files.get("backup")
    if not upload or not upload.filename:
        return page({"backup": "Choose a backup file first."}, 400)
    data = upload.read(EXPORT_LIMIT + 1)
    displays = _ext("displays")
    try:
        result = backup.restore(displays.configs.root, displays.configs, data)
    except backup.BackupError as error:
        return page({"backup": str(error)}, 400)
    words = ["Settings restored."]
    if result["pairings"]:
        words.append(f"{result['pairings']} TV pairing{'s' if result['pairings'] != 1 else ''} restored.")
    words.append("Your old settings are kept in the data folder.")
    flash(" ".join(words), "card-backup:success")
    return redirect(url_for("server.server_page", _anchor="backup"))

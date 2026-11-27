"""Fixes from the open-install review: MAC addresses in backups, the import write order, a repository with
no release yet, the mDNS name in the log, the QR code in the log and on the Server page, and the
SECURE_COOKIES warning (in test_open_install.py). Nothing here touches the network."""

import hashlib
import io
import json
import logging
import urllib.error
import zipfile

import pytest
from test_open_install import HOST_LAN, FakeZeroconf, csrf_of, make
from test_web import PASSWORD, auth

from art_frames import backup, qr, reach, updates
from art_frames.web import create_app

MAC = "AA:BB:CC:DD:EE:01"
SECOND_MAC = "AA:BB:CC:DD:EE:02"
SECOND_ID = "b" * 32


@pytest.fixture
def household(tmp_path):
    app = make(tmp_path)

    def change(raw):
        raw.update(tv_name="Living Room")
        raw["tv"].update(ip="192.168.1.30", mac=MAC)
        raw["displays"] = [{"id": SECOND_ID, "name": "Kitchen", "tv": {"ip": "192.168.1.31", "mac": SECOND_MAC}}]
    app.extensions["configs"].mutate(change)
    return app, tmp_path / "data"


def settings_in(data):
    return json.loads(zipfile.ZipFile(io.BytesIO(data)).read("settings.json"))


# S4: a default export has no TV MAC address. It still has the TV IP addresses.

def test_a_default_export_leaves_out_mac_addresses_but_keeps_ip_addresses(household):
    app, root = household
    data = backup.export(root, app.extensions["configs"])
    settings = settings_in(data)
    assert settings["tv"] == {**settings["tv"], "ip": "192.168.1.30", "mac": ""}
    assert settings["displays"][0]["tv"]["ip"] == "192.168.1.31" and settings["displays"][0]["tv"]["mac"] == ""
    assert MAC.encode() not in data and SECOND_MAC.encode() not in data
    private = settings_in(backup.export(root, app.extensions["configs"], private=True))
    assert private["tv"]["mac"] == MAC and private["displays"][0]["tv"]["mac"] == SECOND_MAC


def test_import_keeps_the_current_mac_address_of_each_tv(household):
    app, root = household
    configs = app.extensions["configs"]
    data = backup.export(root, configs)
    configs.mutate(lambda raw: raw.update(tv_name="Den"))
    backup.restore(root, configs, data)
    cfg = configs.load()
    assert cfg.tv_name == "Living Room" and cfg.tv.mac == MAC and cfg.displays[0].tv.mac == SECOND_MAC


def test_a_backup_without_macs_and_a_wake_schedule_says_what_to_do(household, tmp_path):
    app, root = household
    configs = app.extensions["configs"]
    configs.mutate(lambda raw: raw.update(wake={"enabled": True, "at": "08:00"}))
    assert configs.load().app_schedule.week.has_wake()
    data = backup.export(root, configs)
    other = create_app(tmp_path / "other", password=PASSWORD, testing=True)  # a new install: no MAC to keep
    with pytest.raises(backup.BackupError, match="no MAC address"):
        backup.restore(tmp_path / "other", other.extensions["configs"], data)
    backup.restore(root, configs, data)  # the same install keeps its MAC, so the schedule stays valid
    assert configs.load().app_schedule.week.has_wake()


def test_the_export_text_names_the_ip_addresses(household):
    app, _ = household
    page = app.test_client().get("/server", headers=auth()).text
    assert "and the TV IP addresses" in page


# B6: settings.json is written last; a failed write names the copy of the old settings.

def test_import_writes_settings_last_and_names_the_old_copy_when_a_write_fails(household, monkeypatch):
    app, root = household
    configs = app.extensions["configs"]
    (root / "tokens").mkdir()
    (root / "tokens" / "aa-bb.txt").write_text("pairing-token")
    data = backup.export(root, configs, private=True)
    configs.mutate(lambda raw: raw.update(tv_name="Den"))
    order = []
    real_write, real_save = backup.private_write, configs.save
    monkeypatch.setattr(backup, "private_write", lambda path, body: order.append(path.name) or real_write(path, body))
    monkeypatch.setattr(configs, "save", lambda cfg: order.append("settings.json") or real_save(cfg))
    backup.restore(root, configs, data)
    assert order[0].startswith("settings.before-import-") and order[-1] == "settings.json"
    assert "aa-bb.txt" in order

    def broken(path, body):
        if path.name == "aa-bb.txt":
            raise OSError(28, "No space left on device")
        return real_write(path, body)
    configs.mutate(lambda raw: raw.update(tv_name="Den"))
    monkeypatch.setattr(backup, "private_write", broken)
    with pytest.raises(backup.BackupError, match=r"settings\.before-import-.*\.json in the data folder"):
        backup.restore(root, configs, data)
    assert configs.load().tv_name == "Den"  # settings.json was not written


def test_a_failed_import_shows_the_message_on_the_server_page(household, monkeypatch):
    app, root = household
    client = app.test_client()
    token = csrf_of(client)
    data = backup.export(root, app.extensions["configs"])

    def full(*_):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(app.extensions["configs"], "save", full)
    response = client.post("/server/import", headers=auth(),
                           data={"csrf": token, "backup": (io.BytesIO(data), "backup.zip")})
    assert response.status_code == 400 and "could not finish the import" in response.text


# B3: GitHub answers 404 for a repository with no release yet. That is not a failure.

def not_found(request, timeout):
    raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)


def test_no_release_yet_is_not_a_failure(tmp_path):
    assert updates.fetch_latest(opener=not_found) is None
    with pytest.raises(urllib.error.HTTPError):
        updates.fetch_latest(opener=lambda request, timeout: (_ for _ in ()).throw(
            urllib.error.HTTPError(request.full_url, 500, "Server Error", {}, None)))
    check = updates.Updates(tmp_path, default_on=True, fetch=lambda: None)
    assert check.check()
    view = check.view()
    assert not view["failed"] and not view["available"] and view["latest"] is None and view["checked_at"]


def test_the_server_page_says_there_is_no_release_yet(tmp_path):
    app = make(tmp_path)
    app.extensions["updates"].fetch = lambda: None
    client = app.test_client()
    token = csrf_of(client)
    client.post("/server/updates", headers=auth(), data={"csrf": token, "check": "on"})
    page = client.post("/server/updates/check", headers=auth(), data={"csrf": token}, follow_redirects=True).text
    assert "There is no Art Frames release yet." in page and "could not reach GitHub" not in page


# A11: the log names the registered mDNS service, and warns when zeroconf had to rename it.

class RenamingZeroconf(FakeZeroconf):
    def register_service(self, info, allow_name_change=False):
        info.name = "Art Frames (2)._http._tcp.local."
        super().register_service(info, allow_name_change)


def test_the_log_names_the_mdns_service(caplog):
    caplog.set_level(logging.INFO)
    where = reach.Reach("0.0.0.0", 8765, found=HOST_LAN, bridge=False)
    reach.advertise(where, None, None, factory=FakeZeroconf)
    assert 'art-frames.local, as the web service "Art Frames"' in caplog.text and "already uses" not in caplog.text


def test_a_renamed_mdns_service_is_a_warning_that_names_the_fix(caplog):
    where = reach.Reach("0.0.0.0", 8765, found=HOST_LAN, bridge=False)
    advertiser = reach.advertise(where, None, None, factory=RenamingZeroconf)
    assert advertiser.instance == "Art Frames (2)"
    warning = next(record for record in caplog.records if record.levelno == logging.WARNING)
    assert '"Art Frames (2)"' in warning.getMessage() and "ART_FRAMES_MDNS_NAME" in warning.getMessage()


# A8: the QR code prints only to a terminal; the setup link always prints.

def test_the_log_has_no_qr_code_when_it_is_not_a_terminal():
    where = reach.Reach("0.0.0.0", 8765, found=HOST_LAN, bridge=False)
    text = where.banner("ABCDE-FGHJK", qr_code=False)
    assert "█" not in text and "Scan this" not in text
    assert "http://192.168.1.20:8765/setup?code=ABCDE-FGHJK" in text
    assert "█" in where.banner("ABCDE-FGHJK", qr_code=True)


def test_the_qr_encoder_matches_a_checked_matrix():
    # macOS CoreImage (CIDetector, QR type) decoded a PNG of this exact matrix back to TEXT on 2026-11-03.
    # A change to the encoder that changes this hash needs that check again.
    text = "http://192.168.1.20:8765/setup?code=ABCDE-FGHJK"
    rows = qr.matrix(text)
    bits = "\n".join("".join("1" if cell else "0" for cell in row) for row in rows)
    assert len(rows) == 33  # version 4
    assert hashlib.sha256(bits.encode()).hexdigest() == "fa032f1d245371b89e29d5af00324f2b415e6e2f1aadeeba3906158c4812c66d"


# A6: the Server page QR code uses the number address, as the log does. The .local name stays in the list.

def test_the_server_page_qr_code_uses_the_ip_address(tmp_path):
    where = reach.Reach("0.0.0.0", 8765, found=HOST_LAN, bridge=False, mdns_name="art-frames")
    app = make(tmp_path, reach=where)
    client = app.test_client()
    page = client.get("/server", headers=auth()).text
    assert "http://art-frames.local:8765" in page and "http://192.168.1.20:8765" in page
    svg = client.get("/server/address.svg", headers=auth()).text
    assert svg == qr.svg("http://192.168.1.20:8765", title="Art Frames address")


def test_a_failed_import_with_no_old_copy_says_what_may_have_changed(household, monkeypatch):
    """With no settings.json yet, there is no before-import copy: the message must not name one."""
    app, root = household
    configs = app.extensions["configs"]
    (root / "tokens").mkdir(exist_ok=True)
    (root / "tokens" / "aa-bb.txt").write_text("pairing-token")
    data = backup.export(root, configs, private=True)
    monkeypatch.setattr(configs, "overrides", root / "no-settings-yet.json")
    real_write = backup.private_write

    def broken(path, body):
        if path.name == "aa-bb.txt":
            raise OSError(28, "No space left on device")
        return real_write(path, body)
    monkeypatch.setattr(backup, "private_write", broken)
    with pytest.raises(backup.BackupError) as caught:
        backup.restore(root, configs, data)
    assert "before-import" not in str(caught.value)
    assert "Some layout settings or TV pairings may have changed. The TV settings did not change." in str(caught.value)
    assert not list(root.glob("settings.before-import-*.json"))

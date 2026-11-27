"""Reaching the app (reach.py, qr.py), the update notice (updates.py), Export and Import (backup.py), the
Server page and the start-up banner. Nothing here touches the network: mDNS and GitHub are fakes."""

import io
import json
import zipfile

import pytest
from test_web import PASSWORD, auth

from art_frames import __main__ as entry
from art_frames import backup, qr, reach, updates
from art_frames.web import create_app

HOST_LAN = [("lo", "127.0.0.1"), ("eth0", "192.168.1.20"), ("docker0", "172.17.0.1"), ("veth12", "172.18.0.1")]
BRIDGE = [("lo", "127.0.0.1"), ("eth0", "172.19.0.5")]
SENSOR_KEY = "A" * 43 + "="


def make(tmp_path, **options):
    return create_app(tmp_path / "data", password=PASSWORD, testing=True, **options)


def csrf_of(client):
    client.get("/", headers=auth())
    with client.session_transaction() as saved:
        return saved["csrf"]


# Addresses and the start-up banner.

def test_lan_addresses_skip_loopback_and_docker_networks():
    assert reach.lan_addresses(HOST_LAN) == ["192.168.1.20"]
    assert reach.lan_addresses([("en0", "10.0.0.4"), ("eth1", "172.20.0.9"), ("wlan0", "192.168.4.2")]) == [
        "10.0.0.4", "192.168.4.2", "172.20.0.9"]


def test_bridge_network_detection(tmp_path):
    dockerenv = tmp_path / ".dockerenv"
    assert not reach.in_bridge_network(BRIDGE, dockerenv=str(dockerenv))  # not in a container
    dockerenv.touch()
    assert reach.in_bridge_network(BRIDGE, dockerenv=str(dockerenv))
    assert not reach.in_bridge_network(HOST_LAN, dockerenv=str(dockerenv))  # host networking sees docker0


def test_banner_for_a_lan_install_lists_the_addresses_and_a_qr_code():
    where = reach.Reach("0.0.0.0", 8765, found=HOST_LAN, bridge=False, mdns_name="art-frames")
    text = where.banner()
    assert "http://art-frames.local:8765" in text and "http://192.168.1.20:8765" in text
    assert "172.17.0.1" not in text and "█" in text
    assert "Setup code" not in text


def test_banner_in_setup_mode_has_the_code_and_a_setup_link():
    where = reach.Reach("0.0.0.0", 8765, found=HOST_LAN, bridge=False)
    text = where.banner("ABCDE-FGHJK")
    assert "Setup code: ABCDE-FGHJK" in text
    assert "http://192.168.1.20:8765/setup?code=ABCDE-FGHJK" in text


def test_banner_for_a_local_only_install_prints_no_qr_code():
    # A password-file install binds 127.0.0.1 (host profiles) or runs in a bridge network: no LAN address, no QR code.
    text = reach.Reach("127.0.0.1", 8765, found=HOST_LAN).banner()
    assert "only from this computer: http://127.0.0.1:8765" in text and "█" not in text
    text = reach.Reach("0.0.0.0", 8765, found=BRIDGE, bridge=True).banner()
    assert "http://localhost:8765" in text and "172.19.0.5" not in text


# mDNS: only with host networking and a LAN bind.

@pytest.mark.parametrize("bind, found, bridge, setting, wanted", [
    ("0.0.0.0", HOST_LAN, False, None, True),
    ("0.0.0.0", HOST_LAN, False, "0", False),
    ("0.0.0.0", BRIDGE, True, None, False),
    ("127.0.0.1", HOST_LAN, False, None, False),
    ("127.0.0.1", HOST_LAN, False, "1", False),
    ("0.0.0.0", BRIDGE, True, "1", True),
])
def test_mdns_runs_only_where_it_can_work(bind, found, bridge, setting, wanted):
    where = reach.Reach(bind, 8765, found=found, bridge=bridge)
    assert reach.mdns_wanted(setting, where)[0] is wanted


class FakeZeroconf:
    def __init__(self):
        self.registered, self.closed = [], False

    def register_service(self, info, allow_name_change=False):
        self.registered.append((info, allow_name_change))

    def unregister_service(self, info):
        self.registered.remove(next(item for item in self.registered if item[0] is info))

    def close(self):
        self.closed = True


def test_mdns_registers_the_name_and_the_web_service():
    fake = FakeZeroconf()
    where = reach.Reach("0.0.0.0", 8765, found=HOST_LAN, bridge=False)
    advertiser = reach.advertise(where, None, None, factory=lambda: fake)
    info, rename = fake.registered[0]
    assert info.server == "art-frames.local." and info.port == 8765 and info.parsed_addresses() == ["192.168.1.20"]
    assert info.type == "_http._tcp.local." and rename
    assert where.mdns_name == "art-frames" and where.urls()[0] == "http://art-frames.local:8765"
    advertiser.stop()
    assert fake.closed and not fake.registered


def test_mdns_failure_never_stops_the_app(caplog):
    def broken():
        raise OSError("port 5353 is busy")
    where = reach.Reach("0.0.0.0", 8765, found=HOST_LAN, bridge=False)
    assert reach.advertise(where, None, None, factory=broken) is None
    assert where.mdns_name is None and "mDNS could not start" in caplog.text
    with pytest.raises(ValueError):
        reach.mdns_name("not a name!")
    assert reach.mdns_name("Kitchen-Frames.local") == "kitchen-frames"


# QR codes. scripts check: macOS Vision decoded these same codes during development.

def test_qr_code_shape():
    rows = qr.matrix("http://192.168.1.20:8765")
    assert len(rows) == 25 and all(len(row) == 25 for row in rows)  # version 2
    # The three finder patterns: a dark ring, a light ring and a dark 3x3 centre.
    for x, y in ((0, 0), (18, 0), (0, 18)):
        assert all(rows[y][x + i] for i in range(7)) and not rows[y + 1][x + 1] and rows[y + 3][x + 3]
    assert len(qr.matrix("x" * 200)) == 57  # version 10
    with pytest.raises(ValueError):
        qr.matrix("x" * 300)
    assert qr.svg("http://a").startswith("<svg") and "<path" in qr.svg("http://a")
    assert len(qr.terminal("http://a").splitlines()) == (21 + 4 + 1) // 2


# The Server page.

def test_server_page_shows_the_address_and_serves_its_qr_code(tmp_path):
    where = reach.Reach("0.0.0.0", 8765, found=HOST_LAN, bridge=False)
    client = make(tmp_path, reach=where).test_client()
    page = client.get("/server", headers=auth()).text
    assert "http://192.168.1.20:8765" in page and 'src="/server/address.svg"' in page
    response = client.get("/server/address.svg", headers=auth())
    assert response.mimetype == "image/svg+xml" and response.text.startswith("<svg")
    assert client.get("/server/address.svg").status_code == 401


def test_server_page_for_a_local_only_install(tmp_path):
    client = make(tmp_path).test_client()  # the default: 127.0.0.1, as on a password-file install
    page = client.get("/server", headers=auth()).text
    assert "accepts connections only from the server itself" in page and "address.svg" not in page
    assert 'href="/server?tv=main"' in client.get("/", headers=auth(), follow_redirects=True).text


# The update notice.

class FakeGitHub:
    def __init__(self, tag="v0.2.0", fail=False):
        self.tag, self.fail, self.calls = tag, fail, 0

    def __call__(self):
        self.calls += 1
        if self.fail:
            raise OSError("offline")
        return {"tag": self.tag, "url": updates.RELEASES + "/tag/" + self.tag}


def test_update_check_is_off_by_default_on_a_password_file_install(tmp_path):
    app = make(tmp_path)
    check = app.extensions["updates"]
    assert not check.enabled
    fake = check.fetch = FakeGitHub()
    assert not check.check() and fake.calls == 0
    check.clock = lambda: 10 ** 10
    assert not check.due()
    assert not (tmp_path / "data" / "updates.json").exists()


def test_update_check_is_on_by_default_for_a_new_install(tmp_path):
    app = create_app(tmp_path / "data", testing=True)
    assert app.extensions["updates"].enabled


def test_the_environment_overrides_the_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("ART_FRAMES_UPDATE_CHECK", "1")
    check = make(tmp_path).extensions["updates"]
    assert check.enabled and check.view()["forced"]
    with pytest.raises(ValueError):
        check.set_enabled(False)
    monkeypatch.setenv("ART_FRAMES_UPDATE_CHECK", "0")
    assert not create_app(tmp_path / "new", testing=True).extensions["updates"].enabled


def test_update_notice_when_a_newer_release_is_out(tmp_path):
    app = make(tmp_path)
    client = app.test_client()
    check = app.extensions["updates"]
    fake = check.fetch = FakeGitHub("v99.0.0")
    token = csrf_of(client)
    client.post("/server/updates", headers=auth(), data={"csrf": token, "check": "on"})
    assert check.enabled
    response = client.post("/server/updates/check", headers=auth(), data={"csrf": token}, follow_redirects=True)
    assert fake.calls == 1 and "Art Frames 99.0.0 is out." in response.text
    page = client.get("/server", headers=auth()).text
    assert "Art Frames 99.0.0 is out" in page and "What's new" in page
    # The sidebar shows it on every page.
    assert "Art Frames 99.0.0 is out" in client.get("/household", headers=auth()).text
    # Off again: no notice and no request.
    client.post("/server/updates", headers=auth(), data={"csrf": token})
    assert "is out" not in client.get("/household", headers=auth()).text
    assert not check.check() and fake.calls == 1


def test_a_failed_update_check_is_quiet(tmp_path):
    check = updates.Updates(tmp_path, default_on=True, fetch=FakeGitHub(fail=True))
    assert not check.check()
    assert check.view()["failed"] and not check.view()["available"]


@pytest.mark.parametrize("tag, current, result", [
    ("v0.2.0", "0.1.0", True), ("0.1.0", "0.1.0", False), ("v0.0.9", "0.1.0", False),
    ("v1.0.0-rc1", "0.1.0", False), ("nonsense", "0.1.0", False),
])
def test_version_compare(tag, current, result):
    assert updates.newer(tag, current) is result


def test_tests_never_reach_github(tmp_path):
    check = create_app(tmp_path / "data", testing=True).extensions["updates"]
    assert check.enabled and not check.check()  # the test fetcher raises; check() saves the failure
    assert check.view()["failed"]


# Export and Import.

@pytest.fixture
def household(tmp_path):
    app = make(tmp_path)
    root = tmp_path / "data"

    def change(raw):
        raw.update(tv_name="Living Room", timezone="Europe/Paris")
        raw["sensor"].update(host="192.168.1.50", key=SENSOR_KEY)
    app.extensions["configs"].mutate(change)
    (root / "tokens").mkdir()
    (root / "tokens" / "aa-bb.txt").write_text("pairing-token")
    (root / "legacy-dashboard.json").write_text('{"calendars": ["private-link"]}')
    (root / "layout-settings" / "starter").mkdir(parents=True)
    (root / "layout-settings" / "starter" / "config.json").write_text('{"greeting": "Hello"}')
    return app, root


def names_in(data):
    return sorted(zipfile.ZipFile(io.BytesIO(data)).namelist())


def test_export_leaves_private_values_out_by_default(household):
    app, root = household
    data = backup.export(root, app.extensions["configs"])
    assert names_in(data) == ["manifest.json", "settings.json"]  # no app manifest: its settings stay out
    settings = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("settings.json"))
    assert settings["tv_name"] == "Living Room" and settings["sensor"]["key"] == ""
    assert b"pairing-token" not in data and SENSOR_KEY.encode() not in data


def test_export_with_private_values_and_import_round_trip(household, tmp_path):
    app, root = household
    data = backup.export(root, app.extensions["configs"], private=True)
    assert names_in(data) == ["layout-settings/starter/config.json", "legacy-dashboard.json", "manifest.json",
                              "settings.json", "tokens/aa-bb.txt"]
    other = create_app(tmp_path / "other", password=PASSWORD, testing=True)
    result = backup.restore(tmp_path / "other", other.extensions["configs"], data)
    assert result == {"settings": 1, "apps": 1, "pairings": 1}
    restored = other.extensions["configs"].load()
    assert restored.tv_name == "Living Room" and restored.timezone == "Europe/Paris"
    assert restored.sensor.key.get_secret_value() == SENSOR_KEY
    assert (tmp_path / "other" / "tokens" / "aa-bb.txt").read_text() == "pairing-token"
    assert json.loads((tmp_path / "other" / "layout-settings" / "starter" / "config.json").read_text()) == {
        "greeting": "Hello"}


def test_import_keeps_a_sensor_key_the_backup_left_out_and_a_copy_of_the_old_settings(household):
    app, root = household
    configs = app.extensions["configs"]
    data = backup.export(root, configs)
    configs.mutate(lambda raw: raw.update(tv_name="Kitchen"))
    backup.restore(root, configs, data)
    cfg = configs.load()
    assert cfg.tv_name == "Living Room" and cfg.sensor.key.get_secret_value() == SENSOR_KEY
    kept = list(root.glob("settings.before-import-*.json"))
    assert len(kept) == 1 and json.loads(kept[0].read_text())["tv_name"] == "Kitchen"


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as target:
        for name, text in files.items():
            target.writestr(name, text)
    return stream.getvalue()


MANIFEST = json.dumps({"format": "art-frames-backup", "format_version": 1})


@pytest.mark.parametrize("data, message", [
    (b"not a zip", "not an Art Frames backup"),
    (archive({"settings.json": "{}"}), "not an Art Frames backup"),
    (archive({"manifest.json": MANIFEST, "settings.json": '{"tv": {"ip": "8.8.8.8"}}'}), "not valid"),
    (archive({"manifest.json": MANIFEST, "settings.json": "{}", "../escape.txt": "x"}), "does not know"),
    (archive({"manifest.json": MANIFEST, "settings.json": "{}", "tokens/../../x.txt": "x"}), "does not know"),
    (archive({"manifest.json": json.dumps({"format": "art-frames-backup", "format_version": 9}),
              "settings.json": "{}"}), "newer Art Frames"),
])
def test_a_bad_backup_changes_nothing(household, data, message):
    app, root = household
    before = (root / "settings.json").read_bytes()
    with pytest.raises(backup.BackupError, match=message):
        backup.restore(root, app.extensions["configs"], data)
    assert (root / "settings.json").read_bytes() == before
    assert not list(root.glob("settings.before-import-*.json")) and not (root.parent / "escape.txt").exists()


def test_export_and_import_on_the_server_page(household):
    app, root = household
    client = app.test_client()
    token = csrf_of(client)
    response = client.post("/server/export", headers=auth(), data={"csrf": token})
    assert response.mimetype == "application/zip"
    assert "attachment; filename=art-frames-backup-" in response.headers["Content-Disposition"]
    data = response.data
    assert client.post("/server/export", headers=auth()).status_code == 400  # CSRF
    app.extensions["configs"].mutate(lambda raw: raw.update(tv_name="Kitchen"))
    response = client.post("/server/import", headers=auth(), follow_redirects=True,
                           data={"csrf": token, "backup": (io.BytesIO(data), "backup.zip")})
    assert "Settings restored." in response.text
    assert app.extensions["configs"].load().tv_name == "Living Room"
    response = client.post("/server/import", headers=auth(),
                           data={"csrf": token, "backup": (io.BytesIO(b"nope"), "backup.zip")})
    assert response.status_code == 400 and "not an Art Frames backup" in response.text


# The entry point: the banner, the LAN host rule and a clear error for a data folder it can't write.

@pytest.fixture
def started(monkeypatch, tmp_path):
    calls = {}
    monkeypatch.setattr(entry, "serve", lambda app, **options: calls.update(app=app, **options))
    monkeypatch.setattr(reach, "interfaces", lambda: HOST_LAN)
    monkeypatch.setattr(reach, "in_bridge_network", lambda found=None, **_: False)
    monkeypatch.setattr(reach.Advertiser, "start", lambda self: f"{self.name}.local")
    monkeypatch.setattr("art_frames.displays.Displays.start", lambda self: None)
    monkeypatch.setattr("art_frames.sensor_control.SensorHub.start", lambda self: None)
    monkeypatch.setattr(updates.Updates, "start", lambda self: calls.update(update_thread=True))
    for name in ("HOSTS", "PASSWORD_FILE", "MDNS", "UPDATE_CHECK", "BIND"):
        for key in ("ART_FRAMES_" + name, "DAYFRAME_" + name):
            # setenv first, so monkeypatch also removes what main() sets (DAYFRAME_HOSTS) after the test.
            monkeypatch.setenv(key, "")
            monkeypatch.delenv(key)

    def run(*args):
        monkeypatch.setattr("sys.argv", ["art-frames", "--data-dir", str(tmp_path / "data"), *args])
        entry.main()
        return calls
    return run


def test_a_lan_start_prints_the_setup_link_and_accepts_lan_names(started, capsys, monkeypatch):
    calls = started("--host", "0.0.0.0")
    out = capsys.readouterr().out
    assert "http://art-frames.local:8765" in out and "http://192.168.1.20:8765/setup?code=" in out
    assert "Setup code: " in out and calls["update_thread"]
    import os
    assert os.environ["DAYFRAME_HOSTS"] == "*"
    client = calls["app"].test_client()
    assert client.get("/", headers={"Host": "192.168.1.20:8765"}).status_code == 302
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_a_password_file_start_prints_no_setup_code(started, capsys, monkeypatch, tmp_path):
    secret = tmp_path / "web_password"
    secret.write_text(PASSWORD + "\n")
    monkeypatch.setenv("DAYFRAME_PASSWORD_FILE", str(secret))
    calls = started()
    out = capsys.readouterr().out
    assert "only from this computer: http://127.0.0.1:8765" in out and "Setup code" not in out
    assert calls["app"].extensions["auth"].mode == "file"


def test_a_data_folder_it_cannot_write_names_the_fix(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        message = entry.writable(locked / "data")
    finally:
        locked.chmod(0o700)
    assert message and "cannot write to its data folder" in message and "chown" in message
    assert entry.writable(tmp_path / "fine") is None


def test_main_prints_a_qr_code_only_to_a_terminal(started, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
    started("--host", "0.0.0.0")
    out = capsys.readouterr().out
    assert "/setup?code=" in out and "█" not in out


# A7: SECURE_COOKIES=1 is for a reverse proxy on the same machine. A LAN bind with it gets a warning.

@pytest.mark.parametrize("host, warned", [("0.0.0.0", True), ("127.0.0.1", False)])
def test_secure_cookies_with_a_lan_bind_logs_a_warning(started, monkeypatch, caplog, host, warned):
    monkeypatch.setenv("ART_FRAMES_SECURE_COOKIES", "1")
    started("--host", host)
    assert ("Set ART_FRAMES_BIND=127.0.0.1 with it" in caplog.text) is warned

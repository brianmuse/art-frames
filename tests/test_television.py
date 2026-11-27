import logging
from types import SimpleNamespace

import pytest

from art_frames.config import Config, private_write
from art_frames.television import Television, WakeError


class Art:
    def __init__(self, deleted=True):
        self.deleted = deleted
        self.calls = []

    def get_artmode(self):
        return "on"

    def upload(self, image, *, file_type, matte, portrait_matte):
        self.calls.append(("upload", image))
        return "new-id"

    def select_image(self, content_id, *, show):
        self.calls.append(("select", content_id, show))

    def get_current(self):
        return {"content_id": "new-id"}

    def delete_list(self, content_ids):
        self.calls.append(("delete", content_ids))
        return self.deleted

    def close(self):
        self.calls.append(("close",))


def tv(tmp_path):
    return Television(Config(tv={"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF"}).tv, tmp_path)


@pytest.mark.parametrize("ack,remaining", [(True, ["new-id"]), (False, ["old-id", "new-id"])])
def test_single_upload_and_cleanup_acknowledgement(tmp_path, monkeypatch, ack, remaining):
    device = tv(tmp_path)
    art = Art(deleted=ack)
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(art=lambda **_: art, rest_device_info=lambda: {"device": {"PowerState": "on"}}))
    owned = []
    assert device.publish(b"jpeg", ["old-id"], owned.append) == ("new-id", remaining, True)
    assert owned == ["new-id"]
    assert [call for call in art.calls if call[0] == "upload"] == [("upload", b"jpeg")]
    assert ("select", "new-id", True) in art.calls


def test_unknown_viewing_state_allows_network_wake_but_never_forces_art_mode(tmp_path, monkeypatch):
    device = tv(tmp_path)
    private_write(device.token, b"synthetic-token")

    def failed_status():
        raise OSError("REST endpoint failed")

    art_calls = []
    monkeypatch.setattr(
        device,
        "remote",
        lambda **kwargs: SimpleNamespace(rest_device_info=failed_status, art=lambda **_: art_calls.append(True)),
    )
    packets = fake_wake_network(monkeypatch)
    assert not device.status()["known"]
    with pytest.raises(ValueError, match="unknown"):
        device.wake()
    assert not art_calls
    # One burst, then one more for each of the six checks that cannot reach the TV.
    assert len(packets) == 21 and {target for _, target in packets} == {("255.255.255.255", 9)}


def test_wake_resends_to_configured_and_limited_broadcast(tmp_path, monkeypatch):
    device = Television(Config(tv={"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF",
                                   "broadcast": "192.168.1.255"}).tv, tmp_path)
    replies = [{"paired": True, "known": False, "awake": False, "playing": False}] * 3
    replies.append({"paired": True, "known": True, "awake": True, "playing": False})
    monkeypatch.setattr(device, "status", lambda **kwargs: replies.pop(0))
    packets = fake_wake_network(monkeypatch)
    monkeypatch.setattr(device, "remote", lambda **kwargs: pytest.fail("This path must not force Art Mode"))
    assert device.wake() is True
    targets = [target for _, target in packets]
    # Initial burst plus two resends while unreachable, three packets per target each time.
    assert targets.count(("192.168.1.255", 9)) == 9 and targets.count(("255.255.255.255", 9)) == 9


def test_wake_uses_the_other_broadcast_when_one_fails(tmp_path, monkeypatch):
    device = Television(Config(tv={"ip": "192.168.1.50", "mac": "AA:BB:CC:DD:EE:FF",
                                   "broadcast": "192.168.1.255"}).tv, tmp_path)
    monkeypatch.setattr(device, "status", lambda **kwargs: {"paired": True, "known": True, "awake": True, "playing": False})
    fake_wake_network(monkeypatch)
    packets = []

    class Failing:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, *args): pass
        def sendto(self, packet, target):
            if target[0] == "192.168.1.255":
                raise OSError("unreachable")
            packets.append((packet, target))
    monkeypatch.setattr("art_frames.television.socket.socket", lambda *args: Failing())
    assert device.wake() is True
    assert packets and all(target == ("255.255.255.255", 9) for _, target in packets)


def fake_wake_network(monkeypatch):
    packets = []
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, *args): pass
        def sendto(self, packet, target): packets.append((packet, target))
    monkeypatch.setattr("art_frames.television.socket.socket", lambda *args: Socket())
    monkeypatch.setattr("art_frames.television.time.sleep", lambda seconds: None)
    return packets


@pytest.mark.parametrize("initial,after,expected", [
    ({"known": False, "awake": False, "playing": False}, {"known": True, "awake": True, "playing": False}, True),
    ({"known": False, "awake": False, "playing": False}, {"known": True, "awake": False, "playing": True}, False),
    ({"known": True, "awake": True, "playing": False}, None, True),
    ({"known": True, "awake": True, "playing": True}, None, False),
])
def test_wake_checks_after_magic_packet_without_interrupting_viewing(tmp_path, monkeypatch, initial, after, expected):
    device = tv(tmp_path)
    replies = [{"paired": True, **initial}, {"paired": True, **(after or initial)}]
    monkeypatch.setattr(device, "status", lambda **kwargs: replies.pop(0) if len(replies)>1 else replies[0])
    packets = fake_wake_network(monkeypatch)
    monkeypatch.setattr(device, "remote", lambda **kwargs: pytest.fail("This path must not force Art Mode"))
    assert device.wake() is expected
    assert len(packets) == (0 if initial["playing"] else 3)
    if packets:
        assert len(packets[0][0]) == 102


def test_confirmed_standby_switch_is_verified_not_assumed(tmp_path, monkeypatch):
    device = tv(tmp_path)
    replies = [{"paired": True,"known":True,"awake":False,"playing":False}]*2 + [{"paired":True,"known":True,"awake":True,"playing":False}]
    monkeypatch.setattr(device, "status", lambda **kwargs: replies.pop(0))
    packets = fake_wake_network(monkeypatch)
    commands = []
    monkeypatch.setattr(device, "remote", lambda **kwargs: SimpleNamespace(art=lambda **_: SimpleNamespace(set_artmode=commands.append,close=lambda:None)))
    assert device.wake() is True
    assert commands == [True] and len(packets) == 3


@pytest.mark.parametrize("paired,mac,reason", [(False,"AA:BB:CC:DD:EE:FF","unpaired"),(True,"","missing-mac")])
def test_wake_reports_missing_prerequisites_without_packets(tmp_path, monkeypatch, paired, mac, reason):
    device = Television(Config(tv={"ip":"192.168.1.50","mac":mac}).tv,tmp_path)
    monkeypatch.setattr(device,"status",lambda **kwargs:{"paired":paired,"known":False,"awake":False,"playing":False})
    packets = fake_wake_network(monkeypatch)
    with pytest.raises(WakeError,match=reason):
        device.wake()
    assert not packets


def test_pairing_tokens_are_scoped_to_device_and_not_logged(tmp_path, caplog):
    first = tv(tmp_path)
    second = Television(Config(tv={"ip": "192.168.1.51"}).tv, tmp_path)
    assert first.token != second.token
    first.token.parent.mkdir()
    with caplog.at_level(logging.INFO):
        first.remote()._set_token("SYNTHETIC-PAIRING-SECRET")
    assert "SYNTHETIC-PAIRING-SECRET" not in caplog.text
    assert not second.token.exists()


@pytest.mark.parametrize("power,mode,awake,playing,known,queries", [
    ("", "on", True, False, True, 1),
    (None, "on", True, False, True, 1),
    ("on", "on", True, False, True, 1),
    ("on", "off", False, True, True, 1),
    ("", "off", False, False, False, 1),
    ("on", "unrecognized", False, False, False, 1),
    ("off", "on", False, False, True, 0),
    ("standby", "on", False, False, True, 0),
])
def test_status_uses_art_mode_when_power_field_is_blank(tmp_path, monkeypatch, power, mode, awake, playing, known, queries):
    device = tv(tmp_path)
    private_write(device.token, b"synthetic-token")
    calls = []
    art = SimpleNamespace(get_artmode=lambda: calls.append("query") or mode, close=lambda: calls.append("close"))
    info = {} if power is None else {"PowerState": power}
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(rest_device_info=lambda: {"device": info}, art=lambda **_: art))
    assert device.status() == {"awake": awake, "playing": playing, "paired": True, "known": known, "answered": True}
    assert calls.count("query") == queries
    assert calls.count("close") == queries


def test_status_tells_no_answer_from_an_unclear_answer(tmp_path, monkeypatch):
    """"answered" is True once REST replies, even when the art channel then fails."""
    device = tv(tmp_path)
    private_write(device.token, b"synthetic-token")

    def broken_art(**_):
        raise OSError("art channel failed")
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(
        rest_device_info=lambda: {"device": {"PowerState": "on"}}, art=broken_art))
    assert device.status() == {"awake": False, "playing": False, "paired": True, "known": False, "answered": True}

    def silent():
        raise OSError("no answer")
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(rest_device_info=silent, art=broken_art))
    assert device.status() == {"awake": False, "playing": False, "paired": True, "known": False, "answered": False}
    device.token.unlink()
    assert device.status()["answered"] is False


def test_blank_power_art_mode_can_publish_without_waking(configs, library, monkeypatch):
    from art_frames.controller import Controller

    cfg = Config(tv={"ip": "192.168.1.50"})
    configs.save(cfg)
    device = Television(cfg.tv, configs.root)
    private_write(device.token, b"synthetic-token")
    art = Art()
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(rest_device_info=lambda: {"device": {"PowerState": ""}}, art=lambda **_: art))
    controller = Controller(configs, library, tv_factory=lambda _: device, capture=lambda *a, **kw: b"synthetic-jpeg")
    assert controller.tick(force=True) == "published"
    assert ("select", "new-id", True) in art.calls
    assert library.state("last_display")["device"] == cfg.tv.identity


def test_slow_tv_is_confirmed_within_five_checks(tmp_path, monkeypatch):
    device = tv(tmp_path)
    art = Art()
    replies = iter(["old-id"] * 4 + ["new-id"])
    art.get_current = lambda: {"content_id": next(replies)}
    sleeps = []
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(art=lambda **_: art, rest_device_info=lambda: {"device": {"PowerState": "on"}}))
    monkeypatch.setattr("art_frames.television.time.sleep", sleeps.append)
    assert device.publish(b"jpeg", ["old-id"], lambda _: None) == ("new-id", ["new-id"], True)
    assert sleeps == [1] * 4


@pytest.mark.parametrize("ack,remaining", [(True, ["new-id"]), (False, ["old-id", "new-id"])])
def test_later_cleanup_deletes_only_other_owned_ids_without_mode_changes(tmp_path, monkeypatch, ack, remaining):
    device = tv(tmp_path)
    art = Art(deleted=ack)
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(art=lambda **_: art, rest_device_info=lambda: {"device": {"PowerState": "on"}}))
    assert device.remove_old(["old-id", "new-id"], "new-id") == remaining
    assert art.calls == [("delete", ["old-id"]), ("close",)]
    art.calls.clear()
    assert device.remove_old(["new-id"], "new-id") == ["new-id"] and not art.calls
    monkeypatch.setattr(device, "remote", lambda: (_ for _ in ()).throw(OSError("offline")))
    assert device.remove_old(["old-id", "new-id"], "new-id") == ["old-id", "new-id"]


@pytest.mark.parametrize("power", ["standby", "off", None])
def test_standby_after_upload_never_selects_artwork(tmp_path, monkeypatch, power):
    device = tv(tmp_path)
    art = Art()

    def info():
        if power is None:
            raise OSError("REST endpoint failed")
        return {"device": {"PowerState": power}}

    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(art=lambda **_: art, rest_device_info=info))
    owned = []
    assert device.publish(b"jpeg", ["old-id"], owned.append) is None
    assert owned == ["new-id"]  # ownership is kept so cleanup can remove the upload later
    assert not [call for call in art.calls if call[0] in {"select", "delete"}]


def test_wake_and_publish_record_when_each_step_finished(tmp_path, monkeypatch):
    device = tv(tmp_path)
    clock = iter(range(100))
    monkeypatch.setattr("art_frames.television.time.monotonic", lambda: next(clock))
    replies = [{"paired": True, "known": False, "awake": False, "playing": False}] * 2
    replies += [{"paired": True, "known": True, "awake": False, "playing": False},
                {"paired": True, "known": True, "awake": True, "playing": False}]
    monkeypatch.setattr(device, "status", lambda **kwargs: replies.pop(0))
    fake_wake_network(monkeypatch)
    monkeypatch.setattr(device, "remote", lambda **kwargs: SimpleNamespace(
        art=lambda **_: SimpleNamespace(set_artmode=lambda value: None, close=lambda: None),
        rest_device_info=lambda: {"device": {"PowerState": "on"}}))
    assert device.wake() is True
    assert list(device.phases) == ["packet", "answered", "art_mode"]
    assert device.phases["packet"] < device.phases["answered"] < device.phases["art_mode"]
    art = Art()
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(art=lambda **_: art, rest_device_info=lambda: {"device": {"PowerState": "on"}}))
    device.publish(b"jpeg", [], lambda _: None)
    assert list(device.phases)[3:] == ["upload_start", "upload", "selected", "confirmed"]


def test_upload_is_still_rechecked_and_never_selected_in_standby(tmp_path, monkeypatch):
    device = tv(tmp_path)
    art = Art()
    monkeypatch.setattr(device, "remote", lambda: SimpleNamespace(art=lambda **_: art, rest_device_info=lambda: {"device": {"PowerState": "standby"}}))
    assert device.publish(b"jpeg", [], lambda _: None) is None
    assert not [call for call in art.calls if call[0] == "select"]
    assert "selected" not in device.phases

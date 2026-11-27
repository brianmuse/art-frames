"""NativeClient against a fake aioesphomeapi APIClient with the 46.12 signatures and the library's own
entity and state objects. No network access."""

import asyncio
import inspect
import threading

import pytest

from art_frames import sensors
from art_frames.config import Sensor

aioesphomeapi = pytest.importorskip("aioesphomeapi")


class FakeAPIClient:
    """The parts of aioesphomeapi.APIClient (46.12.1) that NativeClient uses, with the same signatures."""

    made = []
    fail = None  # an exception that connect() raises
    drop_after = 0.05  # seconds until the sensor drops the connection

    def __init__(self, address, port, password=None, *, client_info="aioesphomeapi", keepalive=20.0,
                 zeroconf_instance=None, noise_psk=None, expected_name=None, addresses=None, expected_mac=None,
                 timezone=None, provide_time=True, outgoing_connection_target=False):
        self.address, self.port, self.noise_psk, self.provide_time = address, port, noise_psk, provide_time
        self.on_stop = None
        FakeAPIClient.made.append(self)

    async def connect(self, on_stop=None, login=False, log_errors=True):
        if FakeAPIClient.fail:
            raise FakeAPIClient.fail
        self.on_stop = on_stop

    async def device_info(self):
        return aioesphomeapi.DeviceInfo(name="akamatis-smart-device-a1b2c3", friendly_name="Akamatis Smart Device a1b2c3",
                                        mac_address="02:00:5E:00:53:01", project_name="Akamatis.SmartHomeDevice",
                                        project_version="1.3.0")

    async def list_entities_services(self):
        return [aioesphomeapi.BinarySensorInfo(object_id="presence", key=1, name="Presence", device_class="occupancy"),
                aioesphomeapi.SensorInfo(object_id="ambient_light", key=2, name="Ambient Light",
                                         device_class="illuminance", unit_of_measurement="lx"),
                aioesphomeapi.TextSensorInfo(object_id="mac_address", key=3, name="MAC address")], []

    def subscribe_states(self, on_state):
        on_state(aioesphomeapi.BinarySensorState(key=1, state=True))
        on_state(aioesphomeapi.SensorState(key=2, state=42.5))
        on_state(aioesphomeapi.TextSensorState(key=3, state="02:00:5E:00:53:01"))
        loop = asyncio.get_running_loop()
        loop.call_later(FakeAPIClient.drop_after, lambda: loop.create_task(self.on_stop(False)))

    async def disconnect(self, force=False):
        pass


@pytest.fixture
def fake(monkeypatch):
    FakeAPIClient.made, FakeAPIClient.fail = [], None
    monkeypatch.setattr(aioesphomeapi, "APIClient", FakeAPIClient)
    return FakeAPIClient


def test_the_fake_matches_the_real_signatures():
    for name in ("connect", "device_info", "list_entities_services", "subscribe_states", "disconnect"):
        real = inspect.signature(getattr(aioesphomeapi.APIClient, name))
        assert list(inspect.signature(getattr(FakeAPIClient, name)).parameters) == list(real.parameters)


def test_readings_arrive_and_a_drop_is_lost(fake):
    link = sensors.Link(Sensor(host="192.168.1.148"), clients=[sensors.NativeClient("192.168.1.148", 6053)])
    assert link.run_once() is True
    client = fake.made[0]
    assert client.provide_time is False and client.noise_psk is None
    snap = link.readings.snapshot()
    assert snap["values"][("binary_sensor", "presence")][0] is True
    assert snap["values"][("sensor", "ambient_light")][0] == 42.5
    assert all(domain in sensors.DOMAINS for domain, _ in snap["values"])  # the text sensor is dropped
    assert snap["device"]["name"] == "akamatis-smart-device" and "02:00:5E" not in repr(snap)
    assert link.readings.error == "lost" and not link.readings.connected


def test_a_wrong_key_is_key_wrong(fake):
    fake.fail = aioesphomeapi.InvalidEncryptionKeyAPIError("bad key", received_name="secret-name")
    key = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s="
    link = sensors.Link(Sensor(host="192.168.1.148", key=key))
    assert [client.transport for client in link.clients] == ["api"]
    assert link.run_once() is False
    assert link.readings.error == "key-wrong"
    assert fake.made[0].noise_psk == key


def test_stopping_ends_the_link_without_an_error(fake):
    fake.drop_after = 60
    try:
        link = sensors.Link(Sensor(host="192.168.1.148"), clients=[sensors.NativeClient("192.168.1.148", 6053)])
        threading.Timer(0.2, link.stopping.set).start()
        assert link.run_once() is True
        assert link.readings.error == ""
    finally:
        fake.drop_after = 0.05

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from art_frames.config import Config


def moment(day, clock):
    return datetime.fromisoformat(f"2026-10-{day}T{clock}:00").replace(tzinfo=ZoneInfo("UTC"))


def test_example_matches_documented_defaults():
    import tomllib

    with (Path(__file__).parents[1] / "config.example.toml").open("rb") as stream:
        example = Config.model_validate(tomllib.load(stream))
    assert example == Config()
    assert not example.app_schedule.week.has_wake()


@pytest.mark.parametrize(
    "values",
    [
        {"timezone": "Not/A_Timezone"},
        {"planner": {"start": "7:00"}},
        {"planner": {"days": [7]}},
        {"planner": {"refresh_minutes": 0}},
        {"art": {"sources": ["untrusted"]}},
        {"tv": {"ip": "127.0.0.1"}},
        {"tv": {"ip": "8.8.8.8"}},
        {"tv": {"mac": "bad-mac"}},
        {"weather": {"enabled": True}},
        {"calendars": [{"name": "x", "url": "file:///etc/passwd"}]},
        {"calendars": [{"name": "x", "url": "https://user:password@example.com/calendar"}]},
        {"unknown_setting": True},
    ],
)
def test_invalid_settings_fail_closed(values):
    with pytest.raises(ValidationError):
        Config.model_validate(values)


def test_settings_roundtrip_is_atomic_and_private(configs):
    cfg = Config(timezone="America/New_York", art={"order": "sequential"})
    configs.save(cfg)
    assert configs.load() == cfg
    assert configs.overrides.stat().st_mode & 0o777 == 0o600
    assert not list(configs.root.glob(".writing-*"))

import pytest

from art_frames.env import env, require


def test_the_new_name_wins_over_the_old_name(monkeypatch):
    monkeypatch.setenv("DAYFRAME_PORT", "8000")
    assert env("PORT") == "8000"
    monkeypatch.setenv("ART_FRAMES_PORT", "9000")
    assert env("PORT") == "9000"


def test_a_missing_setting_uses_the_default(monkeypatch):
    monkeypatch.delenv("ART_FRAMES_LOG_LEVEL", raising=False)
    monkeypatch.delenv("DAYFRAME_LOG_LEVEL", raising=False)
    assert env("LOG_LEVEL", "INFO") == "INFO"
    with pytest.raises(KeyError, match="DAYFRAME_LOG_LEVEL"):
        require("LOG_LEVEL")


def test_an_empty_new_value_still_counts(monkeypatch):
    monkeypatch.setenv("DAYFRAME_APPS", "news=news")
    monkeypatch.setenv("ART_FRAMES_APPS", "")
    assert env("APPS") == ""

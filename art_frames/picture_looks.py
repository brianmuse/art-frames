"""Looks for the Picture page: one choice that sets color temperature, brightness, the TV light sensor and mats.

A look fills in normal settings. It is not a mode. When sensor control is on, the sensor owns the brightness
and the TV light sensor, so a look sets only color temperature and mats.

Brightness is a share of the TV's range, like the room presets. Before Art Frames reads the range, a look
uses 0-10, so a TV with a wider range gets a darker screen, never a brighter one.

Nothing here reads or changes the TV. web.py sends the steps through the Controller, one job for all of them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .room_presets import SAFE_RANGE, level


@dataclass(frozen=True)
class Look:
    key: str
    name: str
    line: str
    color: int | None  # color temperature; None leaves it as it is
    percent: int | None  # brightness as a share of the TV's range; None leaves it as it is
    light_sensor: bool  # the TV's own light sensor
    mats: str | None  # "show", "never", or None to leave it
    sensor_line: str = ""  # the line while sensor control is on: it never promises a brightness


LOOKS = (
    Look("warm", "Warm gallery", "Warm color, soft brightness, with mats.", -2, 40, False, "show",
         "Warm color, with mats."),
    Look("neutral", "Neutral", "True color, medium brightness, with mats.", 0, 60, False, "show",
         "True color, with mats."),
    Look("bright", "Bright room", "True color, bright, no mats.", 0, 90, False, "never", "True color, no mats."),
    Look("tv", "Let the TV decide", "The TV's own light sensor sets the brightness.", None, None, True, None),
)
BY_KEY = {look.key: look for look in LOOKS}


def tv_range(display):
    """The TV's brightness range from a stored reading, or 0-10 before Art Frames reads it."""
    low, high = display.get("min"), display.get("max")
    if isinstance(low, int) and isinstance(high, int) and low <= high:
        return low, high
    return SAFE_RANGE


def steps(look, display, sensor_on):
    """The TV changes for a look, in order: [(field, value)]. field is "light_sensor", "brightness" or
    "color_temperature". The light sensor goes first, so it cannot move the brightness that follows."""
    if sensor_on:
        return [("color_temperature", look.color)] if look.color is not None else []
    result = [("light_sensor", look.light_sensor)]
    if look.percent is not None:
        result.append(("brightness", level(look.percent, *tv_range(display))))
    if look.color is not None:
        result.append(("color_temperature", look.color))
    return result


def line(look, sensor_on):
    """The look's line on its card. With sensor control on, the sensor sets the brightness, so it is not named."""
    return look.sensor_line if sensor_on and look.sensor_line else look.line


def usable(look, sensor_on):
    """With sensor control on, the TV light sensor stays off, so "Let the TV decide" cannot apply."""
    return not (sensor_on and look.light_sensor)


def current(display, mats, sensor_on):
    """The look the stored reading and the saved mats match, or None (Custom)."""
    if not display:
        return None
    for look in LOOKS:
        if not usable(look, sensor_on) or (look.mats is not None and look.mats != mats):
            continue
        reading = {"light_sensor": display.get("sensor") == "on", "brightness": display.get("brightness"),
                   "color_temperature": display.get("color_temperature")}
        if all(reading[field] == value for field, value in steps(look, display, sensor_on)):
            if look.key == "tv" and display.get("sensor") != "on":
                continue
            return look
    return None


def shown(field, value):
    """A read-back value in words: "light sensor off", "brightness 4", "color temperature -2"."""
    if field == "light_sensor":
        return f"light sensor {'on' if value in (True, 'on') else 'off'}"
    return f"{field.replace('_', ' ')} {value}"

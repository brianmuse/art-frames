"""Environment settings. Read ART_FRAMES_<NAME> first, then the old DAYFRAME_<NAME>.

Compose, .env and the shell scripts keep writing DAYFRAME_* (see names.py): a rollback
starts old images with the new compose files, and old images read only DAYFRAME_*.
"""

import os

from .names import ENV_PREFIX

PREFIX = "ART_FRAMES_"


def env(name, default=None):
    """The value of ART_FRAMES_<name>, else DAYFRAME_<name>, else `default`."""
    for key in (PREFIX + name, ENV_PREFIX + name):
        value = os.environ.get(key)
        if value is not None:
            return value
    return default


def require(name):
    """Like env(), but a missing setting raises KeyError with the old name, which compose sets."""
    value = env(name)
    if value is None:
        raise KeyError(ENV_PREFIX + name)
    return value

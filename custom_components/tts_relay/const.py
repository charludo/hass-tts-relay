from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .relay import Relay

DOMAIN = "tts_relay"

CONF_ANNOUNCEMENTS = "announcements"
CONF_CONVERT_TO_WAV = "convert_to_wav"
CONF_NAME = "name"
CONF_SATELLITES = "satellites"
CONF_SILENCE = "silence"
CONF_TARGET = "target"
CONF_VOLUME = "volume"
CONF_DUCK = "duck"
CONF_ROUTES = "routes"
CONF_RULES = "rules"
CONF_SOUNDS = "sounds"
CONF_STREAM = "stream"
CONF_YAML = "yaml"

SUBENTRY_ROUTE = "route"

DEFAULT_DUCK = True
DEFAULT_ANNOUNCEMENTS = True
DEFAULT_CONVERT_TO_WAV = False
DEFAULT_SILENCE = False
DEFAULT_STREAM = False

SERVICE_STOP = "stop"
SERVICE_RELOAD = "reload"

DUCK_URL_PATH = f"/{DOMAIN}/duck.mp3"
SOUNDS_URL_PATH = f"/{DOMAIN}/sounds"

DATA_RELAY: HassKey[Relay] = HassKey(DOMAIN)

ISSUE_REPLY_FORMAT = "reply_format"
ISSUE_NEEDS_FFMPEG = "needs_ffmpeg"
PE_DOCS_URL = "https://github.com/charludo/hass-tts-relay#voice-preview-edition"
SILENCE_DOCS_URL = "https://github.com/charludo/hass-tts-relay#silence-the-satellite"

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from .relay import Relay

DOMAIN = "tts_relay"

CONF_NAME = "name"
CONF_SATELLITES = "satellites"
CONF_TARGET = "target"
CONF_VOLUME = "volume"
CONF_DUCK = "duck"
CONF_ROUTES = "routes"
CONF_RULES = "rules"
CONF_SOUNDS = "sounds"

DEFAULT_DUCK = True

SERVICE_STOP = "stop"
SERVICE_RELOAD = "reload"

DUCK_URL_PATH = f"/{DOMAIN}/duck.mp3"
SOUNDS_URL_PATH = f"/{DOMAIN}/sounds"

DATA_RELAY: HassKey[Relay] = HassKey(DOMAIN)

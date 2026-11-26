from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.http import StaticPathConfig
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DUCK,
    CONF_NAME,
    CONF_ROUTES,
    CONF_RULES,
    CONF_SATELLITES,
    CONF_SOUNDS,
    CONF_TARGET,
    CONF_VOLUME,
    DATA_RELAY,
    DEFAULT_DUCK,
    DOMAIN,
    DUCK_URL_PATH,
    SERVICE_RELOAD,
    SERVICE_STOP,
    SOUNDS_URL_PATH,
)
from .relay import Relay, Route, install, route_id
from .rules import async_load_rules, async_load_sounds, user_dir

_LOGGER = logging.getLogger(__name__)


def _distinct(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids: set[str] = set()
    satellites: set[str] = set()
    for route in routes:
        if (id_ := route_id(route[CONF_NAME])) in ids:
            raise vol.Invalid(f"more than one route is named {route[CONF_NAME]!r} ({id_})")
        ids.add(id_)
        for satellite in route[CONF_SATELLITES]:
            if satellite in satellites:
                raise vol.Invalid(f"{satellite} is in more than one route")
            satellites.add(satellite)
    return routes


ROUTE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_SATELLITES): vol.All(
            cv.ensure_list, vol.Length(min=1), [cv.entity_domain("assist_satellite")]
        ),
        vol.Required(CONF_TARGET): cv.entity_domain("media_player"),
        vol.Optional(CONF_VOLUME): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
        vol.Optional(CONF_DUCK, default=DEFAULT_DUCK): cv.boolean,
        vol.Optional(CONF_RULES): [cv.slug],
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_SOUNDS, default={}): {cv.slug: cv.string},
                vol.Optional(CONF_ROUTES, default=[]): vol.All([ROUTE_SCHEMA], _distinct),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

STOP_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids})


def _create_user_dirs(hass: HomeAssistant) -> None:
    for name in ("rules", "sounds"):
        user_dir(hass, name).mkdir(parents=True, exist_ok=True)


def _route(settings: Mapping[str, Any], source: str) -> Route:
    rules = settings.get(CONF_RULES)
    return Route(
        name=settings[CONF_NAME],
        satellites=tuple(settings[CONF_SATELLITES]),
        target=settings[CONF_TARGET],
        volume=settings.get(CONF_VOLUME),
        duck=settings.get(CONF_DUCK, DEFAULT_DUCK),
        rules=None if rules is None else tuple(rules),
        source=source,
    )


def _warn_about_unknown_rules(relay: Relay, route: Route) -> None:
    for rule_id in route.rules or ():
        if rule_id not in relay.rules:
            _LOGGER.warning("Route %s uses reply rule %s, which doesn't exist", route.name, rule_id)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    conf = config.get(DOMAIN, {})
    relay = hass.data[DATA_RELAY] = Relay(hass)
    install()

    await hass.async_add_executor_job(_create_user_dirs, hass)
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(DUCK_URL_PATH, str(Path(__file__).parent / "duck.mp3"), True),
            # Uncached, so a sound replaced under the same name plays.
            StaticPathConfig(SOUNDS_URL_PATH, str(user_dir(hass, "sounds")), False),
        ]
    )

    async def load_rules_and_sounds() -> None:
        relay.rules = await async_load_rules(hass)
        relay.sounds = await async_load_sounds(hass, conf.get(CONF_SOUNDS, {}))
        for route in relay.routes.values():
            _warn_about_unknown_rules(relay, route)

    await load_rules_and_sounds()

    for settings in conf.get(CONF_ROUTES, []):
        route = _route(settings, "yaml")
        _warn_about_unknown_rules(relay, route)
        relay.add_route(route)

    async def stop(call: ServiceCall) -> None:
        await relay.stop(call.data[ATTR_ENTITY_ID])

    async def reload(call: ServiceCall) -> None:
        await load_rules_and_sounds()

    hass.services.async_register(DOMAIN, SERVICE_STOP, stop, schema=STOP_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_RELOAD, reload, schema=vol.Schema({}))
    return True

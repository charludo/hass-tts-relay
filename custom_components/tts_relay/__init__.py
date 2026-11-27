from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import voluptuous as vol
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ANNOUNCEMENTS,
    CONF_CONVERT_TO_WAV,
    CONF_DUCK,
    CONF_NAME,
    CONF_ROUTES,
    CONF_RULES,
    CONF_SATELLITES,
    CONF_SILENCE,
    CONF_SOUNDS,
    CONF_STREAM,
    CONF_TARGET,
    CONF_VOLUME,
    CONF_YAML,
    DATA_RELAY,
    DEFAULT_ANNOUNCEMENTS,
    DEFAULT_CONVERT_TO_WAV,
    DEFAULT_DUCK,
    DEFAULT_SILENCE,
    DEFAULT_STREAM,
    DOMAIN,
    DUCK_URL_PATH,
    SERVICE_RELOAD,
    SERVICE_STOP,
    SOUNDS_URL_PATH,
    SUBENTRY_ROUTE,
)
from .relay import Relay, Route, install, route_id
from .rules import async_load_rules, async_load_sounds, user_dir
from .silence import ReplyView, SilentView, WavView, async_ffmpeg_available

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
        vol.Optional(CONF_ANNOUNCEMENTS, default=DEFAULT_ANNOUNCEMENTS): cv.boolean,
        vol.Optional(CONF_SILENCE, default=DEFAULT_SILENCE): cv.boolean,
        vol.Optional(CONF_STREAM, default=DEFAULT_STREAM): cv.boolean,
        vol.Optional(CONF_CONVERT_TO_WAV, default=DEFAULT_CONVERT_TO_WAV): cv.boolean,
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
        announcements=settings.get(CONF_ANNOUNCEMENTS, DEFAULT_ANNOUNCEMENTS),
        silence=settings.get(CONF_SILENCE, DEFAULT_SILENCE),
        stream=settings.get(CONF_STREAM, DEFAULT_STREAM),
        convert_to_wav=settings.get(CONF_CONVERT_TO_WAV, DEFAULT_CONVERT_TO_WAV),
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
    relay.set_ffmpeg(await async_ffmpeg_available(hass))
    hass.http.register_view(ReplyView(hass))
    hass.http.register_view(SilentView(hass))
    hass.http.register_view(WavView(hass))

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    relay = hass.data[DATA_RELAY]
    _sync_yaml_subentries(hass, entry, relay)
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_ROUTE or subentry.data.get(CONF_YAML):
            continue
        route = _route(subentry.data, "ui")
        in_yaml = [
            satellite
            for satellite in route.satellites
            if (other := relay.route_for(satellite)) and other.source == "yaml"
        ]
        if in_yaml:
            _LOGGER.warning(
                "Route %s: %s routed in YAML, which wins", route.name, ", ".join(in_yaml)
            )
            satellites = tuple(s for s in route.satellites if s not in in_yaml)
            if not satellites:
                continue
            route = dataclasses.replace(route, satellites=satellites)
        _warn_about_unknown_rules(relay, route)
        entry.async_on_unload(relay.add_route(route))
    # Registered after the sync, so it only hears about changes made in the UI.
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


def _yaml_route_data(route: Route) -> dict[str, Any]:
    data: dict[str, Any] = {
        CONF_NAME: route.name,
        CONF_SATELLITES: list(route.satellites),
        CONF_TARGET: route.target,
        CONF_DUCK: route.duck,
        CONF_ANNOUNCEMENTS: route.announcements,
        CONF_SILENCE: route.silence,
        CONF_STREAM: route.stream,
        CONF_CONVERT_TO_WAV: route.convert_to_wav,
        CONF_YAML: True,
    }
    if route.volume is not None:
        data[CONF_VOLUME] = route.volume
    if route.rules is not None:
        data[CONF_RULES] = list(route.rules)
    return data


@callback
def _sync_yaml_subentries(hass: HomeAssistant, entry: ConfigEntry, relay: Relay) -> None:
    """Mirror the YAML routes as subentries, so the UI lists them."""
    yaml_routes = {id_: route for id_, route in relay.routes.items() if route.source == "yaml"}
    by_id = {sub.unique_id: sub for sub in entry.subentries.values()}
    for id_, route in yaml_routes.items():
        data = _yaml_route_data(route)
        title = f"{route.name} (YAML)"
        subentry = by_id.get(id_)
        if subentry is None:
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(data),
                    subentry_type=SUBENTRY_ROUTE,
                    title=title,
                    unique_id=id_,
                ),
            )
            continue
        if not subentry.data.get(CONF_YAML):
            _LOGGER.warning("Route %s is now set in YAML, which replaces the UI one", route.name)
        hass.config_entries.async_update_subentry(entry, subentry, data=data, title=title)
    for subentry in list(entry.subentries.values()):
        if subentry.data.get(CONF_YAML) and subentry.unique_id not in yaml_routes:
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True

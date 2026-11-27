from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .const import (
    CONF_ANNOUNCEMENTS,
    CONF_CONVERT_TO_WAV,
    CONF_DUCK,
    CONF_NAME,
    CONF_RULES,
    CONF_SATELLITES,
    CONF_SILENCE,
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
    SUBENTRY_ROUTE,
)
from .relay import route_id
from .rules import Rule, by_priority

SATELLITES_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="assist_satellite", multiple=True)
)
TARGET_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(
        filter=selector.EntityFilterSelectorConfig(
            domain="media_player",
            supported_features=["media_player.MediaPlayerEntityFeature.MEDIA_ANNOUNCE"],
        )
    )
)
VOLUME_PERCENT_SELECTOR = selector.NumberSelector(
    selector.NumberSelectorConfig(
        min=0, max=100, step=1, unit_of_measurement="%", mode=selector.NumberSelectorMode.BOX
    )
)


def _rules(hass: HomeAssistant) -> list[Rule]:
    relay = hass.data.get(DATA_RELAY)
    return by_priority(relay.rules.values()) if relay else []


def _rule_descriptions(hass: HomeAssistant) -> dict[str, str]:
    # Into the form's text, since a select's options only have room for names.
    lines = [f"- **{rule.name}**: {rule.description}" for rule in _rules(hass)]
    return {"rules": "\n".join(lines) or "-"}


def _settings_schema(hass: HomeAssistant, current: Mapping[str, Any]) -> dict:
    rules = _rules(hass)
    chosen_rules = current.get(CONF_RULES)
    if chosen_rules is None:
        chosen_rules = [rule.id for rule in rules if rule.default]
    volume = current.get(CONF_VOLUME)
    return {
        vol.Required(CONF_TARGET, default=current.get(CONF_TARGET, vol.UNDEFINED)): (
            TARGET_SELECTOR
        ),
        vol.Optional(
            CONF_VOLUME,
            description={"suggested_value": None if volume is None else round(volume * 100)},
        ): VOLUME_PERCENT_SELECTOR,
        vol.Optional(CONF_DUCK, default=current.get(CONF_DUCK, DEFAULT_DUCK)): (
            selector.BooleanSelector()
        ),
        vol.Optional(
            CONF_ANNOUNCEMENTS,
            default=current.get(CONF_ANNOUNCEMENTS, DEFAULT_ANNOUNCEMENTS),
        ): selector.BooleanSelector(),
        vol.Optional(CONF_SILENCE, default=current.get(CONF_SILENCE, DEFAULT_SILENCE)): (
            selector.BooleanSelector()
        ),
        vol.Optional(CONF_STREAM, default=current.get(CONF_STREAM, DEFAULT_STREAM)): (
            selector.BooleanSelector()
        ),
        vol.Optional(
            CONF_CONVERT_TO_WAV,
            default=current.get(CONF_CONVERT_TO_WAV, DEFAULT_CONVERT_TO_WAV),
        ): selector.BooleanSelector(),
        vol.Optional(CONF_RULES, default=chosen_rules): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=rule.id, label=rule.name) for rule in rules
                ],
                multiple=True,
                mode=selector.SelectSelectorMode.LIST,
            )
        ),
    }


def _route_schema(hass: HomeAssistant, current: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, vol.UNDEFINED)): (
                selector.TextSelector()
            ),
            vol.Required(CONF_SATELLITES, default=current.get(CONF_SATELLITES, vol.UNDEFINED)): (
                SATELLITES_SELECTOR
            ),
            **_settings_schema(hass, current),
        }
    )


def _route_data(form: Mapping[str, Any]) -> dict[str, Any]:
    data = {
        CONF_NAME: form[CONF_NAME].strip(),
        CONF_SATELLITES: list(form.get(CONF_SATELLITES, [])),
        CONF_TARGET: form[CONF_TARGET],
        CONF_DUCK: form.get(CONF_DUCK, DEFAULT_DUCK),
        CONF_ANNOUNCEMENTS: form.get(CONF_ANNOUNCEMENTS, DEFAULT_ANNOUNCEMENTS),
        CONF_SILENCE: form.get(CONF_SILENCE, DEFAULT_SILENCE),
        CONF_STREAM: form.get(CONF_STREAM, DEFAULT_STREAM),
        CONF_CONVERT_TO_WAV: form.get(CONF_CONVERT_TO_WAV, DEFAULT_CONVERT_TO_WAV),
        CONF_RULES: list(form.get(CONF_RULES, [])),
    }
    if form.get(CONF_VOLUME) is not None:
        data[CONF_VOLUME] = form[CONF_VOLUME] / 100
    return data


def _check(
    hass: HomeAssistant,
    data: Mapping[str, Any],
    entry: ConfigEntry | None = None,
    editing: str | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Return errors and their placeholders if the route clashes with another."""
    ids: set[str] = set()
    owners: dict[str, str] = {}  # satellite -> name of the route it's in
    relay = hass.data.get(DATA_RELAY)
    for route in relay.routes.values() if relay else ():
        if route.source == "yaml":
            ids.add(route.id)
            owners.update(dict.fromkeys(route.satellites, route.name))
    for subentry in entry.subentries.values() if entry else ():
        if subentry.subentry_id == editing or subentry.data.get(CONF_YAML):
            continue
        ids.add(route_id(subentry.data[CONF_NAME]))
        owners.update(dict.fromkeys(subentry.data[CONF_SATELLITES], subentry.data[CONF_NAME]))

    errors: dict[str, str] = {}
    if route_id(data[CONF_NAME]) in ids:
        errors[CONF_NAME] = "name_taken"
    taken = [f"{sat} ({owners[sat]})" for sat in data[CONF_SATELLITES] if sat in owners]
    if not data[CONF_SATELLITES]:
        errors[CONF_SATELLITES] = "no_satellites"
    elif taken:
        errors[CONF_SATELLITES] = "satellite_taken"
    return errors, {"taken": ", ".join(taken)}


def _ffmpeg_note(hass: HomeAssistant) -> str:
    relay = hass.data.get(DATA_RELAY)
    if relay is None or relay.ffmpeg_ok:
        return ""
    return (
        "\n\n**ffmpeg isn't available**, so *Silence the satellites* and "
        "*Convert replies to WAV* won't work. Home Assistant normally comes with ffmpeg."
    )


def _show_route_form(
    flow: ConfigFlow | ConfigSubentryFlow,
    step_id: str,
    current: Mapping[str, Any],
    errors: dict[str, str] | None = None,
    placeholders: dict[str, str] | None = None,
) -> Any:
    return flow.async_show_form(
        step_id=step_id,
        data_schema=_route_schema(flow.hass, current),
        errors=errors,
        description_placeholders={
            **_rule_descriptions(flow.hass),
            "taken": "",
            "name": current.get(CONF_NAME, ""),
            "ffmpeg": _ffmpeg_note(flow.hass),
            **(placeholders or {}),
        },
    )


class TtsRelayConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["add_route", "finish"])

    async def async_step_add_route(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return _show_route_form(self, "add_route", {})
        data = _route_data(user_input)
        errors, placeholders = _check(self.hass, data)
        if errors:
            return _show_route_form(self, "add_route", data, errors, placeholders)
        subentry = ConfigSubentryData(
            data=data,
            subentry_type=SUBENTRY_ROUTE,
            title=data[CONF_NAME],
            unique_id=route_id(data[CONF_NAME]),
        )
        return self.async_create_entry(title="TTS Relay", data={}, subentries=[subentry])

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(title="TTS Relay", data={})

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_ROUTE: RouteSubentryFlow}


class RouteSubentryFlow(ConfigSubentryFlow):
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is None:
            return _show_route_form(self, "user", {})
        data = _route_data(user_input)
        errors, placeholders = _check(self.hass, data, self._get_entry())
        if errors:
            return _show_route_form(self, "user", data, errors, placeholders)
        return self.async_create_entry(
            title=data[CONF_NAME], data=data, unique_id=route_id(data[CONF_NAME])
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        if subentry.data.get(CONF_YAML):
            return self.async_abort(reason="managed_in_yaml")
        if user_input is None:
            return _show_route_form(self, "reconfigure", subentry.data)
        data = _route_data(user_input)
        entry = self._get_entry()
        errors, placeholders = _check(self.hass, data, entry, editing=subentry.subentry_id)
        if errors:
            return _show_route_form(self, "reconfigure", data, errors, placeholders)
        # The entry's update listener reloads it.
        return self.async_update_and_abort(
            entry,
            subentry,
            title=data[CONF_NAME],
            data=data,
            unique_id=route_id(data[CONF_NAME]),
        )

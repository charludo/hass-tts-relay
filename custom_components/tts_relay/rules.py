from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, TemplateError
from homeassistant.helpers.template import Template
from homeassistant.util.yaml import load_yaml

from .const import DOMAIN, SOUNDS_URL_PATH

_LOGGER = logging.getLogger(__name__)

SHIPPED_RULES = Path(__file__).parent / "rules"
SOUND_SUFFIXES = {".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac"}

DROP = "drop"
SPEECH = "speech"

RULE_SCHEMA = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Optional("description", default=""): str,
        vol.Optional("default", default=False): bool,
        vol.Optional("priority", default=100): int,
        vol.Required("reply"): str,
    }
)


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    name: str
    description: str
    default: bool
    priority: int
    reply: Template


@dataclass(frozen=True, slots=True)
class Reply:
    url: str | None
    rule: str


def user_dir(hass: HomeAssistant, name: str) -> Path:
    return Path(hass.config.path(DOMAIN, name))


def by_priority(rules: Iterable[Rule]) -> list[Rule]:
    return sorted(rules, key=lambda rule: (rule.priority, rule.id))


def _read_rule_files(directories: list[Path]) -> dict[str, tuple[Path, Any]]:
    found: dict[str, tuple[Path, Any]] = {}
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")):
            try:
                found[path.stem] = (path, load_yaml(path))
            except HomeAssistantError as err:
                _LOGGER.error("Can't read reply rule %s: %s", path, err)
    return found


async def async_load_rules(hass: HomeAssistant) -> dict[str, Rule]:
    files = await hass.async_add_executor_job(
        _read_rule_files, [SHIPPED_RULES, user_dir(hass, "rules")]
    )
    rules: dict[str, Rule] = {}
    for rule_id, (path, content) in files.items():
        try:
            config = RULE_SCHEMA(content)
            reply = Template(config["reply"], hass)
            reply.ensure_valid()
        except (vol.Invalid, TemplateError) as err:
            _LOGGER.error("Ignoring reply rule %s: %s", path, err)
            continue
        rules[rule_id] = Rule(
            id=rule_id,
            name=config["name"],
            description=config["description"],
            default=config["default"],
            priority=config["priority"],
            reply=reply,
        )
    _LOGGER.debug("Reply rules: %s", ", ".join(sorted(rules)) or "none")
    return rules


def _read_sound_files(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    return {
        path.stem: f"{SOUNDS_URL_PATH}/{path.name}"
        for path in sorted(directory.iterdir())
        if path.suffix.lower() in SOUND_SUFFIXES
    }


async def async_load_sounds(hass: HomeAssistant, configured: dict[str, str]) -> dict[str, str]:
    found = await hass.async_add_executor_job(_read_sound_files, user_dir(hass, "sounds"))
    return found | configured


def variables(
    *,
    intent_output: dict[str, Any] | None,
    satellite: str,
    target: str,
    tts_url: str | None,
    sounds: dict[str, str],
    acknowledged: bool,
) -> dict[str, Any]:
    response = (intent_output or {}).get("response") or {}
    speech = ((response.get("speech") or {}).get("plain") or {}).get("speech")
    error_code = (
        (response.get("data") or {}).get("code")
        if response.get("response_type") == "error"
        else None
    )
    card = (response.get("card") or {}).get(DOMAIN)
    return {
        "intent_output": intent_output,
        "response": response,
        "speech": speech,
        "error_code": error_code,
        "card": card or None,
        "acknowledged": acknowledged,
        "sounds": sounds,
        "satellite": satellite,
        "target": target,
        "tts_url": tts_url,
    }


def decide(rules: list[Rule], values: dict[str, Any], sounds: dict[str, str]) -> Reply | None:
    for rule in rules:
        try:
            result = rule.reply.async_render(values, parse_result=False).strip()
        except TemplateError as err:
            _LOGGER.warning("Reply rule %s failed: %s", rule.id, err)
            continue
        if not result:
            continue
        if result == DROP:
            return Reply(None, rule.id)
        if result == SPEECH:
            return Reply(values["tts_url"], rule.id)
        if result in sounds:
            return Reply(sounds[result], rule.id)
        if result.startswith(("/", "http://", "https://")):
            return Reply(result, rule.id)
        _LOGGER.warning(
            "Reply rule %s chose %r, which is no sound, URL, 'drop' or 'speech'",
            rule.id,
            result,
        )
    return None

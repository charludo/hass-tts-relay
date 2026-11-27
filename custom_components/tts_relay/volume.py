from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from functools import wraps
from typing import Any

from homeassistant.components.media_player import ATTR_MEDIA_VOLUME_LEVEL
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

_LOGGER = logging.getLogger(__name__)

# How long to wait for a player to report the restored volume.
RESTORE_TIMEOUT = 2.0
# Players round, e.g. to whole percent.
TOLERANCE = 0.01

_ours: ContextVar[bool] = ContextVar("tts_relay_ours", default=False)
# The step of the relative volume intent running, e.g. 0.1 for "louder by 10".
_relative_step: ContextVar[float | None] = ContextVar("tts_relay_relative_step", default=None)

_WRAPPED = ("async_set_volume_level", "async_volume_up", "async_volume_down")


def media_player(hass: HomeAssistant, entity_id: str) -> Any | None:
    component = hass.data.get("media_player")
    return component.get_entity(entity_id) if component else None


async def set_volume(hass: HomeAssistant, entity_id: str, volume: float) -> None:
    """Set the volume as us, which passes through a hold."""
    token = _ours.set(True)
    try:
        await hass.services.async_call(
            "media_player",
            "volume_set",
            {"entity_id": entity_id, ATTR_MEDIA_VOLUME_LEVEL: volume},
            blocking=True,
        )
    except Exception:
        _LOGGER.exception("Setting the volume of %s failed", entity_id)
    finally:
        _ours.reset(token)


class Hold:
    def __init__(self, hass: HomeAssistant, entity_id: str, volume: float) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self.volume = volume  # what to restore to
        self.restoring = False
        self._entity = media_player(hass, entity_id)
        self._wrappers: dict[str, Callable[..., Coroutine[Any, Any, None]]] = {}
        if self._entity is not None:
            self._install()

    @property
    def held(self) -> bool:
        """Whether changes from others reach us, so waiting for the player pays off."""
        return bool(self._wrappers)

    def _install(self) -> None:
        entity = self._entity
        original_set = entity.async_set_volume_level
        originals = {name: getattr(entity, name) for name in _WRAPPED}

        async def change(volume: float) -> None:
            self.volume = min(1.0, max(0.0, volume))
            _LOGGER.debug("%s: will restore to %s", self.entity_id, self.volume)
            if self.restoring:
                await original_set(self.volume)

        @wraps(originals["async_set_volume_level"])
        async def set_volume_level(volume: float) -> None:
            if _ours.get():
                await original_set(volume)
            elif (step := _relative_step.get()) is not None:
                await change(self.volume + step)
            else:
                await change(volume)

        @wraps(originals["async_volume_up"])
        async def volume_up() -> None:
            await change(self.volume + entity.volume_step)

        @wraps(originals["async_volume_down"])
        async def volume_down() -> None:
            await change(self.volume - entity.volume_step)

        self._wrappers = {
            "async_set_volume_level": set_volume_level,
            "async_volume_up": volume_up,
            "async_volume_down": volume_down,
        }
        # On the instance, so only this player and only while held.
        for name, wrapper in self._wrappers.items():
            setattr(entity, name, wrapper)

    def release(self) -> None:
        for name, wrapper in self._wrappers.items():
            if self._entity.__dict__.get(name) is wrapper:
                delattr(self._entity, name)
        self._wrappers = {}

    def _reported(self) -> bool:
        state = self.hass.states.get(self.entity_id)
        level = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL) if state else None
        return level is not None and abs(level - self.volume) < TOLERANCE

    async def until_reported(self) -> None:
        """Wait until the player reports the volume we restore to."""
        if self._reported():
            return
        done = asyncio.Event()

        @callback
        def check(_event: Event[EventStateChangedData]) -> None:
            if self._reported():
                done.set()

        unsub = async_track_state_change_event(self.hass, [self.entity_id], check)
        try:
            async with asyncio.timeout(RESTORE_TIMEOUT):
                await done.wait()
        except TimeoutError:
            _LOGGER.debug("%s didn't report volume %s in time", self.entity_id, self.volume)
        finally:
            unsub()


def install() -> None:
    try:
        from homeassistant.components.media_player.intent import (
            MediaSetVolumeRelativeHandler,
        )
    except ImportError:
        _LOGGER.warning("Can't find the relative volume intent; steps while ducked may be off")
        return
    original = MediaSetVolumeRelativeHandler.async_handle
    if getattr(original, "_tts_relay", False):
        return

    @wraps(original)
    async def async_handle(self, intent_obj):
        step = None
        try:
            value = self.async_validate_slots(intent_obj.slots)["volume_step"]["value"]
            if isinstance(value, int | float):
                step = float(value)
        except Exception:  # noqa: BLE001
            pass
        token = _relative_step.set(step)
        try:
            return await original(self, intent_obj)
        finally:
            _relative_step.reset(token)

    async_handle._tts_relay = True  # type: ignore[attr-defined]
    MediaSetVolumeRelativeHandler.async_handle = async_handle

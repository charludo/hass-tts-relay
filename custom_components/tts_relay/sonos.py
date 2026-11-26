from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


def speaker(hass: HomeAssistant, entity_id: str) -> Any | None:
    component = hass.data.get("media_player")
    entity = component.get_entity(entity_id) if component else None
    return getattr(entity, "speaker", None)


def is_sonos(hass: HomeAssistant, entity_id: str) -> bool:
    return speaker(hass, entity_id) is not None


def last_announce_id(hass: HomeAssistant, entity_id: str) -> str | None:
    return getattr(speaker(hass, entity_id), "last_announce_id", None)


async def cancel_clip(hass: HomeAssistant, entity_id: str, clip_id: str | None = None) -> None:
    sonos = speaker(hass, entity_id)
    websocket = getattr(sonos, "websocket", None)
    clip_id = clip_id or getattr(sonos, "last_announce_id", None)
    if websocket is None or not clip_id:
        return
    try:
        response, _ = await websocket.cancel_clip(clip_id)
    except Exception:
        _LOGGER.exception("Cancelling announcement %s on %s failed", clip_id, entity_id)
        return
    if not response.get("success"):
        # Usually a clip that has already ended.
        _LOGGER.debug("Sonos rejected cancelling %s on %s: %s", clip_id, entity_id, response)

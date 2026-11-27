from __future__ import annotations

import logging

from homeassistant.components.assist_satellite.entity import AssistSatelliteState
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_MUTED,
    MediaPlayerEntityFeature,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The command is over, or the satellite is gone.
_DONE = {AssistSatelliteState.IDLE, STATE_UNAVAILABLE, STATE_UNKNOWN}


class Silencer:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[list[str]] = Store(hass, 1, f"{DOMAIN}.muted")
        self._muted: dict[str, str] = {}  # player -> satellite
        self._watching: dict[str, CALLBACK_TYPE] = {}  # by satellite
        self._left_muted: set[str] = set()  # by a previous run, still to unmute

    async def async_load(self) -> None:
        """Unmute players a previous run of Home Assistant left muted."""
        for player in await self._store.async_load() or []:
            self._left_muted.add(player)
            self._unmute_once_available(player)

    def players(self, satellite: str, exclude: str) -> list[str]:
        """The satellite's own media players."""
        registry = er.async_get(self.hass)
        entry = registry.async_get(satellite)
        if entry is None or entry.device_id is None:
            return []
        return [
            player.entity_id
            for player in er.async_entries_for_device(registry, entry.device_id)
            if player.domain == "media_player" and player.entity_id != exclude
        ]

    async def silence(self, satellite: str, exclude: str) -> None:
        """Mute the satellite's speaker until it's done, e.g. its reply ended."""
        if satellite in self._watching:
            return  # Muted already, e.g. the conversation goes on.
        players = [p for p in self.players(satellite, exclude) if self._can_mute(p)]
        if not players:
            return
        for player in players:
            self._muted[player] = satellite
        self._save()
        self._watching[satellite] = async_track_state_change_event(
            self.hass, [satellite], self._satellite_changed
        )
        await self._set_muted(players, True)

    def _can_mute(self, player: str) -> bool:
        state = self.hass.states.get(player)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return False
        if state.attributes.get(ATTR_MEDIA_VOLUME_MUTED):
            return False  # Muted by someone else, who'll want it to stay that way.
        if player in self._muted:
            return False
        features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        return bool(features & MediaPlayerEntityFeature.VOLUME_MUTE)

    @callback
    def _satellite_changed(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data["new_state"]
        if new_state is None or new_state.state in _DONE:
            self._unsilence(event.data["entity_id"])

    @callback
    def _unsilence(self, satellite: str) -> None:
        if (unsubscribe := self._watching.pop(satellite, None)) is not None:
            unsubscribe()
        players = [p for p, s in self._muted.items() if s == satellite]
        for player in players:
            del self._muted[player]
        self._save()
        self.hass.async_create_background_task(
            self._set_muted(players, False), name=f"tts_relay.unmute[{satellite}]"
        )

    @callback
    def _unmute_once_available(self, player: str) -> None:
        unsubscribe: CALLBACK_TYPE | None = None

        @callback
        def check(_event: Event[EventStateChangedData] | None = None) -> None:
            state = self.hass.states.get(player)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                return
            if unsubscribe is not None:
                unsubscribe()

            async def unmute() -> None:
                await self._set_muted([player], False)
                self._left_muted.discard(player)
                self._save()

            self.hass.async_create_background_task(unmute(), name=f"tts_relay.unmute[{player}]")

        state = self.hass.states.get(player)
        if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            check()
            return
        unsubscribe = async_track_state_change_event(self.hass, [player], check)

    async def _set_muted(self, players: list[str], muted: bool) -> None:
        if not players:
            return
        try:
            await self.hass.services.async_call(
                "media_player",
                "volume_mute",
                {ATTR_ENTITY_ID: players, ATTR_MEDIA_VOLUME_MUTED: muted},
                blocking=True,
            )
        except Exception:
            _LOGGER.exception("%s %s failed", "Muting" if muted else "Unmuting", players)

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(lambda: sorted(self._muted.keys() | self._left_muted), 0)

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import wraps
from typing import Any

from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.components.assist_satellite.entity import AssistSatelliteEntity
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import slugify

from . import sonos
from .const import DATA_RELAY, DUCK_URL_PATH
from .rules import Rule, by_priority, decide, variables

_LOGGER = logging.getLogger(__name__)

DUCKED_VOLUME = 0.3


@dataclass(frozen=True, slots=True)
class Route:
    name: str
    satellites: tuple[str, ...]
    target: str
    volume: float | None = None
    duck: bool = True
    rules: tuple[str, ...] | None = None  # None: the rules that are on by default
    source: str = "ui"

    @property
    def id(self) -> str:
        return route_id(self.name)


def route_id(name: str) -> str:
    return slugify(name)


class Relay:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.routes: dict[str, Route] = {}  # by ID
        self._by_satellite: dict[str, Route] = {}
        self.rules: dict[str, Rule] = {}
        self.sounds: dict[str, str] = {}
        self._intent_output: dict[str, dict[str, Any] | None] = {}
        self._acknowledged: set[str] = set()
        self._ducks: dict[str, asyncio.Task[str | None]] = {}
        self._lowered: dict[str, float] = {}
        self._duckers: dict[str, set[str]] = {}

    @callback
    def add_route(self, route: Route) -> CALLBACK_TYPE:
        self.routes[route.id] = route
        for satellite in route.satellites:
            self._by_satellite[satellite] = route
        _LOGGER.info(
            "Relaying %s to %s (%s, %s)",
            ", ".join(route.satellites),
            route.target,
            route.name,
            route.source,
        )

        @callback
        def remove() -> None:
            if self.routes.get(route.id) is route:
                del self.routes[route.id]
            for satellite in route.satellites:
                if self._by_satellite.get(satellite) is route:
                    del self._by_satellite[satellite]

        return remove

    def route_for(self, satellite: str) -> Route | None:
        return self._by_satellite.get(satellite)

    def rules_for(self, route: Route) -> list[Rule]:
        if route.rules is None:
            return by_priority(rule for rule in self.rules.values() if rule.default)
        return by_priority(self.rules[r] for r in route.rules if r in self.rules)

    @callback
    def handle_event(self, satellite: str, event: PipelineEvent) -> None:
        route = self._by_satellite.get(satellite)
        if route is None:
            return
        data = event.data or {}

        match event.type:
            # Not wake_word-end! satellites detect the wake word themselves and
            # start the pipeline at speech-to-text, so it never fires for them.
            case PipelineEventType.RUN_START:
                self._intent_output.pop(satellite, None)
                self._acknowledged.discard(satellite)
                if route.duck:
                    self._duck(route, satellite)
            # Not stt-end, which waits for the transcript. Run end and errors too,
            # so an aborted run doesn't leave the music down for 15 s.
            case (
                PipelineEventType.STT_VAD_END | PipelineEventType.RUN_END | PipelineEventType.ERROR
            ):
                self._release(route.target, satellite)
            case PipelineEventType.INTENT_END:
                self._intent_output[satellite] = data.get("intent_output")
            case PipelineEventType.TTS_START:
                if data.get("acknowledge_override"):
                    self._acknowledged.add(satellite)
                else:
                    self._acknowledged.discard(satellite)
            case PipelineEventType.TTS_END:
                url = self._reply_url(
                    route,
                    satellite,
                    self._intent_output.pop(satellite, None),
                    (data.get("tts_output") or {}).get("url"),
                    acknowledged=satellite in self._acknowledged,
                )
                self._acknowledged.discard(satellite)
                if url is None:
                    self._release(route.target, satellite)
                else:
                    others = self._leave(route.target, satellite)
                    self._run(self._replace_duck(route, url, others), "reply", route.target)

    def _duck(self, route: Route, satellite: str) -> None:
        duckers = self._duckers.setdefault(route.target, set())
        others = bool(duckers - {satellite})
        duckers.add(satellite)
        if others:
            return  # Ducked already, for another satellite.
        if sonos.is_sonos(self.hass, route.target):
            self._start_duck(route)
        else:
            self._lower_volume(route.target)

    def _leave(self, target: str, satellite: str) -> bool:
        """Take the satellite off the target's duckers, return whether others remain."""
        duckers = self._duckers.get(target, set())
        duckers.discard(satellite)
        if not duckers:
            self._duckers.pop(target, None)
        return bool(duckers)

    def _release(self, target: str, satellite: str) -> None:
        if satellite in self._duckers.get(target, ()) and not self._leave(target, satellite):
            self._run(self._clear_duck(target), "clear_duck", target)

    def _reply_url(
        self,
        route: Route,
        satellite: str,
        intent_output: dict[str, Any] | None,
        tts_url: str | None,
        *,
        acknowledged: bool,
    ) -> str | None:
        url = tts_url
        if rules := self.rules_for(route):
            values = variables(
                intent_output=intent_output,
                satellite=satellite,
                target=route.target,
                tts_url=tts_url,
                sounds=self.sounds,
                acknowledged=acknowledged,
            )
            if (reply := decide(rules, values, self.sounds)) is not None:
                _LOGGER.debug("%s: rule %s plays %s", satellite, reply.rule, reply.url)
                url = reply.url
        return self._absolute(url) if url else None

    def _absolute(self, url: str) -> str | None:
        # HA hands out paths like /api/tts_proxy/<token>.mp3.
        if url.startswith(("http://", "https://")):
            return url
        try:
            base = get_url(self.hass, allow_internal=True, prefer_external=False)
        except NoURLAvailableError:
            _LOGGER.error("No Home Assistant URL configured, can't relay %s", url)
            return None
        return base.rstrip("/") + url

    def _run(self, coro: Coroutine[Any, Any, None], what: str, target: str) -> None:
        self.hass.async_create_background_task(coro, name=f"tts_relay.{what}[{target}]")

    async def announce(self, route: Route, url: str) -> None:
        data: dict[str, Any] = {
            "entity_id": route.target,
            "announce": True,
            "media_content_id": url,
            "media_content_type": "music",
        }
        if route.volume is not None and sonos.is_sonos(self.hass, route.target):
            data["extra"] = {"volume": round(route.volume * 100)}
        try:
            await self.hass.services.async_call("media_player", "play_media", data, blocking=True)
        except Exception:
            _LOGGER.exception("Playing %s on %s failed", url, route.target)

    def _start_duck(self, route: Route) -> None:
        url = self._absolute(DUCK_URL_PATH)
        if url is None:
            return
        previous = self._ducks.pop(route.target, None)

        async def play() -> str | None:
            if previous is not None:
                await self._cancel(route.target, await previous)
            before = sonos.last_announce_id(self.hass, route.target)
            await self.announce(route, url)
            clip_id = sonos.last_announce_id(self.hass, route.target)
            # Unchanged means the clip didn't start, so there's nothing of ours to cancel.
            return clip_id if clip_id != before else None

        self._ducks[route.target] = self.hass.async_create_background_task(
            play(), name=f"tts_relay.duck[{route.target}]"
        )

    def _lower_volume(self, target: str) -> None:
        state = self.hass.states.get(target)
        if state is None or state.state != MediaPlayerState.PLAYING or target in self._lowered:
            return
        volume = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
        features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        if volume is None or not features & MediaPlayerEntityFeature.VOLUME_SET:
            return
        self._lowered[target] = volume
        self._run(self._set_volume(target, volume * DUCKED_VOLUME), "duck", target)

    async def _set_volume(self, target: str, volume: float) -> None:
        try:
            await self.hass.services.async_call(
                "media_player",
                "volume_set",
                {"entity_id": target, ATTR_MEDIA_VOLUME_LEVEL: volume},
                blocking=True,
            )
        except Exception:
            _LOGGER.exception("Setting the volume of %s failed", target)

    async def _clear_duck(self, target: str) -> None:
        if (volume := self._lowered.pop(target, None)) is not None:
            await self._set_volume(target, volume)
        task = self._ducks.pop(target, None)
        if task is None:
            return
        # A short command can end before Sonos has started the clip. Waiting keeps
        # the cancel from overtaking it on the websocket, which takes one call at a time.
        await self._cancel(target, await task)

    async def _replace_duck(self, route: Route, url: str, others: bool = False) -> None:
        # Cancel first, or the reply would wait for the duck clip to end.
        # Other speakers get their volume back too, or the reply would be quiet.
        await self._clear_duck(route.target)
        await self.announce(route, url)
        if others and sonos.is_sonos(self.hass, route.target):
            # Another satellite is still listening.
            self._start_duck(route)

    async def _cancel(self, target: str, clip_id: str | None) -> None:
        if clip_id:
            await sonos.cancel_clip(self.hass, target, clip_id)

    async def stop(self, entity_ids: list[str]) -> None:
        for entity_id in entity_ids:
            self._duckers.pop(entity_id, None)
            if (task := self._ducks.pop(entity_id, None)) is not None:
                task.cancel()
        await self.hass.services.async_call(
            "media_player", "media_stop", {"entity_id": entity_ids}, blocking=True
        )
        # On Sonos, a playing announcement survives media_stop.
        for entity_id in entity_ids:
            await sonos.cancel_clip(self.hass, entity_id)


def install() -> None:
    """Wrap the satellite entity."""
    _wrap_pipeline_events()


def _relay_for(entity: AssistSatelliteEntity) -> Relay | None:
    return entity.hass.data.get(DATA_RELAY) if entity.hass else None


def _wrap_pipeline_events() -> None:
    original: Callable[[Any, PipelineEvent], None] = (
        AssistSatelliteEntity._internal_on_pipeline_event
    )
    if getattr(original, "_tts_relay", False):
        return

    @wraps(original)
    def on_pipeline_event(self, event: PipelineEvent) -> None:
        original(self, event)
        if (relay := _relay_for(self)) is None:
            return
        try:
            relay.handle_event(self.entity_id, event)
        except Exception:
            _LOGGER.exception("Handling %s for %s failed", event.type, self.entity_id)

    on_pipeline_event._tts_relay = True  # type: ignore[attr-defined]
    AssistSatelliteEntity._internal_on_pipeline_event = on_pipeline_event

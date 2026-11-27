from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from homeassistant.components import tts
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.components.assist_satellite.entity import (
    AssistSatelliteAnnouncement,
    AssistSatelliteEntity,
)
from homeassistant.components.media_player import (
    ATTR_MEDIA_VOLUME_LEVEL,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.util import slugify

from . import sonos, volume
from .const import (
    DATA_RELAY,
    DOMAIN,
    DUCK_URL_PATH,
    ISSUE_REPLY_FORMAT,
    PE_DOCS_URL,
    SOUNDS_URL_PATH,
)
from .rules import Rule, by_priority, decide, user_dir, variables
from .silence import Silencer

_LOGGER = logging.getLogger(__name__)

DUCKED_VOLUME = 0.3
# The same announcement for several satellites of one route plays once.
ANNOUNCEMENT_REPEAT_WINDOW = 5.0  # seconds
# What a satellite plays instead of a reply we drop.
SILENCE = Path(__file__).parent / "silence.mp3"
# Sonos doesn't play other formats as an announcement.
SONOS_CLIP_FORMATS = frozenset({".mp3", ".wav"})


@dataclass(frozen=True, slots=True)
class Route:
    name: str
    satellites: tuple[str, ...]
    target: str
    volume: float | None = None
    duck: bool = True
    rules: tuple[str, ...] | None = None  # None: the rules that are on by default
    announcements: bool = True
    silence: bool = False  # mute the satellites' own speakers while they reply
    stream: bool = False  # play streamed (LLM) replies while they're being written
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
        self._holds: dict[str, volume.Hold] = {}  # volume ducks, by target
        self._duckers: dict[str, set[str]] = {}
        self._announced: dict[tuple[str, str], float] = {}  # (target, what) -> when
        self._bad_format: set[str] = set()  # satellites already warned about
        self.silencer = Silencer(hass)
        self._stream_url: dict[str, str] = {}  # the run's reply, if it may stream
        self._streamed: set[str] = set()  # satellites whose reply plays already

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
                self._streamed.discard(satellite)
                tts_output = data.get("tts_output") or {}
                if route.stream and tts_output.get("stream_response") and tts_output.get("url"):
                    self._stream_url[satellite] = tts_output["url"]
                else:
                    self._stream_url.pop(satellite, None)
                if route.silence:
                    # Long before the satellite gets the reply to play.
                    self._run(self.silencer.silence(satellite, route.target), "silence", satellite)
                if route.duck:
                    self._duck(route, satellite)
            case (
                PipelineEventType.STT_VAD_END
                | PipelineEventType.STT_END
                | PipelineEventType.RUN_END
                | PipelineEventType.ERROR
            ):
                self._release(route.target, satellite)
            case PipelineEventType.INTENT_PROGRESS:
                # The agent has written enough that Home Assistant starts speaking
                # before it's done. The satellite starts playing now, too.
                if data.get("tts_start_streaming") and (
                    tts_url := self._stream_url.pop(satellite, None)
                ):
                    self._stream(route, satellite, tts_url)
            case PipelineEventType.INTENT_END:
                self._intent_output[satellite] = data.get("intent_output")
            case PipelineEventType.TTS_START:
                if data.get("acknowledge_override"):
                    self._acknowledged.add(satellite)
                else:
                    self._acknowledged.discard(satellite)
            case PipelineEventType.TTS_END:
                self._stream_url.pop(satellite, None)
                if satellite in self._streamed:
                    # Playing since the agent started writing.
                    self._streamed.discard(satellite)
                    self._intent_output.pop(satellite, None)
                    self._acknowledged.discard(satellite)
                    return
                tts_output = data.get("tts_output") or {}
                tts_url = tts_output.get("url")
                self._check_format(route, satellite, tts_url)
                url = self._reply_url(
                    route,
                    satellite,
                    self._intent_output.pop(satellite, None),
                    tts_url,
                    acknowledged=satellite in self._acknowledged,
                )
                self._acknowledged.discard(satellite)
                if tts_url and url != self._absolute(tts_url):
                    self._mirror_on_satellite(satellite, tts_output.get("token"), url)
                self._play_reply(route, satellite, url)

    def _mirror_on_satellite(self, satellite: str, token: str | None, url: str | None) -> None:
        try:
            stream = tts.async_get_stream(self.hass, token) if token else None
        except KeyError:  # No text-to-speech set up.
            stream = None
        if stream is None:
            return
        path = self._local_file(url) if url else None
        _LOGGER.debug("%s plays %s instead of the speech", satellite, path or "silence")
        # Like Home Assistant's own acknowledge sound, in the satellite's format.
        stream.async_override_result(path or SILENCE)

    def _local_file(self, url: str) -> Path | None:
        """The file on disk behind a sound's URL, if it's one Home Assistant serves."""
        parsed = urlparse(url)
        base = self._absolute("/")
        if parsed.netloc and (base is None or parsed.netloc != urlparse(base).netloc):
            return None
        relative = PurePosixPath(parsed.path)
        for prefix, folder in (
            (PurePosixPath(SOUNDS_URL_PATH), user_dir(self.hass, "sounds")),
            (PurePosixPath("/local"), Path(self.hass.config.path("www"))),
        ):
            if relative.is_relative_to(prefix):
                path = (folder / relative.relative_to(prefix)).resolve()
                if path.is_relative_to(folder.resolve()) and path.is_file():
                    return path
        return None

    def _stream(self, route: Route, satellite: str, tts_url: str) -> None:
        """Play the reply while the agent is still writing it, if the rules allow.

        The rules can't see the response yet. If one decides anyway, e.g. on the
        time of day, we wait for the response and let them decide as usual.
        """
        url = self._reply_url(route, satellite, None, tts_url, acknowledged=False)
        if url is None or url != self._absolute(tts_url):
            return
        self._check_format(route, satellite, tts_url)
        _LOGGER.debug("%s: the agent streams, playing its reply early", satellite)
        self._streamed.add(satellite)
        self._play_reply(route, satellite, url)

    def _play_reply(self, route: Route, satellite: str, url: str | None) -> None:
        if url is None:
            self._release(route.target, satellite)
        else:
            others = self._leave(route.target, satellite)
            self._run(self._replace_duck(route, url, others), "reply", route.target)

    @callback
    def handle_announcement(
        self, satellite: str, announcement: AssistSatelliteAnnouncement
    ) -> None:
        route = self._by_satellite.get(satellite)
        if route is None or not route.announcements:
            return
        now = time.monotonic()
        self._announced = {
            key: when
            for key, when in self._announced.items()
            if now - when < ANNOUNCEMENT_REPEAT_WINDOW
        }
        # Satellites may speak it in different voices, so the text identifies it.
        key = (route.target, announcement.message or announcement.original_media_id)
        if key in self._announced:
            return
        self._announced[key] = now
        if announcement.media_id_source == "tts":
            self._check_format(route, satellite, announcement.media_id)
        if (url := self._absolute(announcement.media_id)) is not None:
            self._run(self.announce(route, url), "announcement", route.target)

    def _check_format(self, route: Route, satellite: str, tts_url: str | None) -> None:
        """Raise a repair if the satellite asks for speech the target can't play.

        The satellite picks the format, e.g. FLAC on the Voice PE.
        """
        if not tts_url or not sonos.is_sonos(self.hass, route.target):
            return
        issue_id = f"{ISSUE_REPLY_FORMAT}_{satellite}"
        extension = PurePosixPath(urlparse(tts_url).path).suffix.lower()
        if not extension or extension in SONOS_CLIP_FORMATS:
            self._bad_format.discard(satellite)
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        if satellite not in self._bad_format:
            self._bad_format.add(satellite)
            _LOGGER.warning(
                "%s asks for %s speech, which %s can't play as an announcement. See %s",
                satellite,
                extension,
                route.target,
                PE_DOCS_URL,
            )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            learn_more_url=PE_DOCS_URL,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_REPLY_FORMAT,
            translation_placeholders={
                "satellite": satellite,
                "target": route.target,
                "format": extension.removeprefix(".").upper(),
            },
        )

    async def silence_announcement(self, satellite: str) -> None:
        """Mute the satellite's own speaker for an announcement we relay."""
        route = self._by_satellite.get(satellite)
        if route is not None and route.announcements and route.silence:
            await self.silencer.silence(satellite, route.target)

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
        elif self._is_music_assistant(route.target):
            # Music Assistant plays a chime before announcements by default.
            data["extra"] = {"use_pre_announce": False}
        try:
            await self.hass.services.async_call("media_player", "play_media", data, blocking=True)
        except Exception:
            _LOGGER.exception("Playing %s on %s failed", url, route.target)

    def _is_music_assistant(self, entity_id: str) -> bool:
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry is not None and entry.platform == "music_assistant"

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
        if (hold := self._holds.get(target)) is not None:
            if not hold.restoring:
                return  # Ducked already.
            # Back down before the player reported the restore. It may still
            # report the ducked volume, so keep what to restore to.
            hold.restoring = False
        else:
            state = self.hass.states.get(target)
            if state is None or state.state != MediaPlayerState.PLAYING:
                return
            level = state.attributes.get(ATTR_MEDIA_VOLUME_LEVEL)
            features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
            if level is None or not features & MediaPlayerEntityFeature.VOLUME_SET:
                return
            hold = self._holds[target] = volume.Hold(self.hass, target, level)
        self._run(volume.set_volume(self.hass, target, hold.volume * DUCKED_VOLUME), "duck", target)

    async def _restore_volume(self, target: str) -> None:
        hold = self._holds.get(target)
        if hold is None or hold.restoring:
            return
        hold.restoring = True
        await volume.set_volume(self.hass, target, hold.volume)
        if hold.held:
            # A "quieter" right after reads the player's volume, so changes keep
            # going to the hold until the player reports the restored one.
            # Not waited for here, or the reply would wait too.
            self._run(self._end_hold(target, hold), "end_hold", target)
        elif self._holds.get(target) is hold:
            del self._holds[target]

    async def _end_hold(self, target: str, hold: volume.Hold) -> None:
        await hold.until_reported()
        if self._holds.get(target) is hold and hold.restoring:
            hold.release()
            del self._holds[target]

    async def _clear_duck(self, target: str) -> None:
        await self._restore_volume(target)
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
    """Wrap the satellite entity, and the relative volume intent."""
    _wrap_pipeline_events()
    _wrap_announcements()
    volume.install()


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
        if (relay := _relay_for(self)) is not None:
            try:
                relay.handle_event(self.entity_id, event)
            except Exception:
                _LOGGER.exception("Handling %s for %s failed", event.type, self.entity_id)
        original(self, event)

    on_pipeline_event._tts_relay = True  # type: ignore[attr-defined]
    AssistSatelliteEntity._internal_on_pipeline_event = on_pipeline_event


def _wrap_announcements() -> None:
    original: Callable[..., Coroutine[Any, Any, AssistSatelliteAnnouncement]] = (
        AssistSatelliteEntity._resolve_announcement_media_id
    )
    if getattr(original, "_tts_relay", False):
        return

    @wraps(original)
    async def resolve_announcement(self, *args: Any, **kwargs: Any) -> AssistSatelliteAnnouncement:
        announcement = await original(self, *args, **kwargs)
        # A satellite already announcing refuses this one right after.
        if (relay := _relay_for(self)) is None or self._is_announcing:
            return announcement
        try:
            relay.handle_announcement(self.entity_id, announcement)
            # Waited for: the satellite starts playing as soon as this returns.
            await relay.silence_announcement(self.entity_id)
        except Exception:
            _LOGGER.exception("Relaying an announcement for %s failed", self.entity_id)
        return announcement

    resolve_announcement._tts_relay = True  # type: ignore[attr-defined]
    AssistSatelliteEntity._resolve_announcement_media_id = resolve_announcement

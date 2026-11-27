import asyncio
import io
import math
import struct
import sys
import wave
from pathlib import Path

import aiohttp
import pytest
from conftest import BASE_URL, setup_relay, start_hass
from homeassistant.components import tts
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.setup import async_setup_component

SAT = "assist_satellite.living_room"
SPEAKER = "media_player.living_room"
ROUTE = {"name": "Living room", "satellites": [SAT], "target": SPEAKER, "silence": True}
RATE = 16000


def tone(seconds: float, rate: int = RATE) -> bytes:
    """A loud WAV, nothing like silence."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / rate)))
                for i in range(int(seconds * rate))
            )
        )
    return buffer.getvalue()


ENGINE = """
import io, math, struct, wave
from homeassistant.components.tts import Provider


class Engine(Provider):
    name = "Test"
    default_language = "en"
    supported_languages = ["en"]

    def get_tts_audio(self, message, language, options):
        # A second of tone for every ten characters.
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as file:
            file.setnchannels(1)
            file.setsampwidth(2)
            file.setframerate(16000)
            file.writeframes(b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 16000)))
                for i in range(len(message) * 1600)
            ))
        return "wav", buffer.getvalue()


async def async_get_engine(hass, config, discovery_info=None):
    return Engine()
"""


@pytest.fixture
def tone_tts_integration(tmp_path):
    integration = tmp_path / "custom_components" / "tone_tts"
    integration.mkdir(parents=True)
    (integration / "manifest.json").write_text(
        '{"domain": "tone_tts", "name": "Tone TTS", "version": "1.0",'
        ' "documentation": "", "codeowners": [], "requirements": []}'
    )
    (integration / "tts.py").write_text(ENGINE)
    if (package := sys.modules.get("custom_components")) is not None:
        package.__path__.append(str(integration.parent))


@pytest.fixture
async def hass(tone_tts_integration, tmp_path):
    hass = await start_hass(tmp_path)
    assert await async_setup_component(hass, "tts", {"tts": {"platform": "tone_tts"}})
    await hass.async_block_till_done()
    yield hass
    await hass.async_stop(force=True)


def reply(hass, extension: str, text: str = "x" * 20) -> tts.ResultStream:
    """A reply as the pipeline makes it for a satellite: two seconds of speech."""
    stream = tts.async_create_stream(hass, "tone_tts", "en", {"preferred_format": extension})
    stream.async_set_message(text)
    return stream


async def decode(audio: bytes) -> tuple[float, int]:
    """Seconds and loudest sample."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1", "-ar", str(RATE),
        "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )  # fmt: skip
    pcm, _ = await process.communicate(audio)
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[: len(pcm) // 2 * 2])
    return len(samples) / RATE, max((abs(s) for s in samples), default=0)


async def read(stream: tts.ResultStream) -> bytes:
    return b"".join([chunk async for chunk in stream.async_stream_result()])


async def fetch(hass, url: str) -> tuple[int, bytes]:
    if not hass.is_running:
        await hass.async_start()
    if url.startswith(BASE_URL):
        url = f"http://127.0.0.1:{hass.data['test_port']}" + url.removeprefix(BASE_URL)
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        return response.status, await response.read()


async def start(hass, relay, stream):
    tts_output = {"url": stream.url, "token": stream.token, "stream_response": False}
    for event in (
        PipelineEvent(PipelineEventType.RUN_START, {"tts_output": tts_output}),
        PipelineEvent(PipelineEventType.INTENT_END, {"intent_output": {"response": {}}}),
        PipelineEvent(PipelineEventType.TTS_END, {"tts_output": tts_output}),
    ):
        relay.handle_event(SAT, event)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.parametrize("extension", ["flac", "mp3", "wav"])
async def test_the_satellite_plays_its_reply_silently(hass, players, extension):
    relay = await setup_relay(hass, players, [ROUTE])
    stream = reply(hass, extension)
    await start(hass, relay, stream)

    # The speaker gets the real reply, from our view.
    [url] = players.urls()
    assert url == f"{BASE_URL}/api/tts_relay/reply/{stream.token}"
    status, audio = await fetch(hass, url)
    assert status == 200
    real, loudest = await decode(audio)
    assert loudest > 10000
    assert real == pytest.approx(2.0, abs=0.1)

    # The satellite reads the stream, through tts_proxy or by token.
    seconds, loudest = await decode(await read(stream))
    assert loudest == 0
    # MP3 pads each encoding a little.
    assert seconds == pytest.approx(real, abs=0.1)


async def test_only_when_turned_on(hass, players):
    relay = await setup_relay(hass, players, [{**ROUTE, "silence": False}])
    stream = reply(hass, "wav")
    await start(hass, relay, stream)
    assert players.urls() == [BASE_URL + stream.url]
    assert (await decode(await read(stream)))[1] > 10000


async def test_a_chime_a_rule_chose_is_silent_too(hass, players):
    relay = await setup_relay(hass, players, [{**ROUTE, "rules": ["chime_on_error"]}])
    Path(hass.config.path("tts_relay", "sounds", "error.wav")).write_bytes(tone(0.5))
    await hass.services.async_call("tts_relay", "reload", blocking=True)
    stream = reply(hass, "flac")
    tts_output = {"url": stream.url, "token": stream.token}
    error = {"response_type": "error", "data": {"code": "failed_to_handle"}}
    for event in (
        PipelineEvent(PipelineEventType.RUN_START, {"tts_output": tts_output}),
        PipelineEvent(PipelineEventType.INTENT_END, {"intent_output": {"response": error}}),
        PipelineEvent(PipelineEventType.TTS_END, {"tts_output": tts_output}),
    ):
        relay.handle_event(SAT, event)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert players.urls()[-1].endswith("/tts_relay/sounds/error.wav")
    seconds, loudest = await decode(await read(stream))
    assert (loudest, seconds) == (0, pytest.approx(0.5, abs=0.1))


async def test_a_streamed_reply_starts_on_the_satellite_before_it_ends(hass):
    from custom_components.tts_relay.silence import silent_copy

    flac = await _encode(tone(10.0), "flac")
    more = asyncio.Event()

    async def generated():
        yield flac[: len(flac) // 2]
        await more.wait()  # The agent is still writing.
        yield flac[len(flac) // 2 :]

    copy = silent_copy(hass, generated(), "flac")
    first = await asyncio.wait_for(anext(copy), timeout=5)
    assert first
    more.set()
    rest = b"".join([chunk async for chunk in copy])
    seconds, loudest = await decode(first + rest)
    assert (loudest, seconds) == (0, pytest.approx(10.0, abs=0.1))


async def test_wav_headers_survive_any_chunking(hass):
    from custom_components.tts_relay.silence import silent_copy

    audio = tone(0.3)

    async def in_bytes():
        for i in range(len(audio)):
            yield audio[i : i + 1]

    silent = b"".join([chunk async for chunk in silent_copy(hass, in_bytes(), "wav")])
    assert silent[:44] == audio[:44]
    assert silent[44:] == bytes(len(audio) - 44)


async def test_tts_announcements_are_silent_on_the_satellite(hass, players):
    from homeassistant.components.assist_satellite.entity import AssistSatelliteAnnouncement

    relay = await setup_relay(hass, players, [ROUTE])
    stream = reply(hass, "flac", "x" * 10)
    announcement = AssistSatelliteAnnouncement(
        message="x" * 10,
        media_id=BASE_URL + stream.url,
        original_media_id="media-source://tts/...",
        tts_token=stream.token,
        media_id_source="tts",
    )
    # As the satellite entity does with every announcement.
    quiet = relay.quiet_announcement(SAT, announcement)
    relay.handle_announcement(SAT, announcement)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert quiet.media_id == announcement.media_id
    seconds, loudest = await decode(await read(stream))
    assert (loudest, seconds) == (0, pytest.approx(1.0, abs=0.1))
    [url] = players.urls()
    assert url == f"{BASE_URL}/api/tts_relay/reply/{stream.token}"
    assert (await decode((await fetch(hass, url))[1]))[1] > 10000


async def test_file_announcements_and_their_chime_get_a_silent_copy(hass, players):
    from test_relay import satellite_entity

    www = Path(hass.config.path("www"))
    www.mkdir()
    (www / "doorbell.wav").write_bytes(tone(1.5))
    chime = await _encode(tone(0.5), "mp3")
    (www / "chime.mp3").write_bytes(chime)
    await setup_relay(hass, players, [ROUTE])
    announcement = await satellite_entity(hass, SAT)._resolve_announcement_media_id(
        "Doorbell", "/local/doorbell.wav", preannounce_media_id="/local/chime.mp3"
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert players.urls() == [BASE_URL + "/local/doorbell.wav"]
    chime_length = (await decode(chime))[0]  # MP3 pads each encoding a little.
    for url, length in (
        (announcement.media_id, 1.5),
        (announcement.preannounce_media_id, chime_length),
    ):
        assert "/api/tts_relay/silent/" in url and "authSig=" in url
        status, audio = await fetch(hass, url)
        assert status == 200
        seconds, loudest = await decode(audio)
        assert (loudest, seconds) == (0, pytest.approx(length, abs=0.1))


async def test_silent_copies_need_our_signature(hass, players):
    from custom_components.tts_relay.silence import silent_url

    await setup_relay(hass, players, [ROUTE])
    url = silent_url(hass, "/local/doorbell.wav")
    tampered = url.replace("doorbell", "secrets")
    assert (await fetch(hass, BASE_URL + tampered))[0] == 401
    assert (await fetch(hass, BASE_URL + url.split("&authSig")[0]))[0] == 401


async def test_heard_rather_than_missing_when_no_copy_can_be_made(hass, players):
    from custom_components.tts_relay.silence import silent_url

    www = Path(hass.config.path("www"))
    www.mkdir()
    (www / "broken.mp3").write_bytes(b"not audio at all")
    await setup_relay(hass, players, [ROUTE])
    status, audio = await fetch(hass, BASE_URL + silent_url(hass, "/local/broken.mp3"))
    assert (status, audio) == (200, b"not audio at all")


async def _encode(wav: bytes, extension: str) -> bytes:
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-i", "pipe:0", "-f", extension, "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )  # fmt: skip
    audio, _ = await process.communicate(wav)
    return audio


def no_ffmpeg(monkeypatch):
    from custom_components.tts_relay import silence

    monkeypatch.setattr(silence, "_binary", lambda hass: "/nonexistent/ffmpeg")


def ffmpeg_issue(hass):
    from homeassistant.helpers import issue_registry as ir

    return ir.async_get(hass).async_get_issue("tts_relay", "silence_needs_ffmpeg")


async def test_without_ffmpeg_satellites_are_heard_and_the_rest_works(
    hass, players, monkeypatch, caplog
):
    no_ffmpeg(monkeypatch)
    relay = await setup_relay(hass, players, [ROUTE])
    assert ffmpeg_issue(hass).translation_placeholders == {"routes": "Living room"}
    assert "ffmpeg isn't available" in caplog.text

    stream = reply(hass, "flac")
    await start(hass, relay, stream)
    assert players.urls() == [BASE_URL + stream.url]  # The reply as usual.
    assert (await decode(await read(stream)))[1] > 10000

    from homeassistant.components.assist_satellite.entity import AssistSatelliteAnnouncement

    announcement = AssistSatelliteAnnouncement(
        message="Doorbell",
        media_id=BASE_URL + "/local/doorbell.wav",
        original_media_id="/local/doorbell.wav",
        tts_token=None,
        media_id_source="url",
        preannounce_media_id=BASE_URL + "/local/chime.mp3",
    )
    assert relay.quiet_announcement(SAT, announcement) is announcement


async def test_the_repair_follows_the_routes_and_ffmpeg(hass, players, monkeypatch):
    from custom_components.tts_relay.relay import Route

    no_ffmpeg(monkeypatch)
    relay = await setup_relay(hass, players, [{**ROUTE, "silence": False}])
    assert ffmpeg_issue(hass) is None

    kitchen = Route("Kitchen", ("assist_satellite.kitchen",), SPEAKER, silence=True)
    remove = relay.add_route(kitchen)
    assert ffmpeg_issue(hass).translation_placeholders == {"routes": "Kitchen"}
    relay.set_ffmpeg(True)
    assert ffmpeg_issue(hass) is None
    relay.set_ffmpeg(False)
    remove()
    assert ffmpeg_issue(hass) is None


async def test_ffmpeg_gone_later_means_heard_not_missing(hass, players, monkeypatch):
    relay = await setup_relay(hass, players, [ROUTE])
    assert ffmpeg_issue(hass) is None
    stream = reply(hass, "flac")
    await start(hass, relay, stream)
    no_ffmpeg(monkeypatch)  # E.g. uninstalled since.

    assert (await decode(await read(stream)))[1] > 10000
    assert ffmpeg_issue(hass) is not None
    later = reply(hass, "flac", "y" * 20)
    await start(hass, relay, later)
    assert players.urls()[-1] == BASE_URL + later.url


async def test_the_route_form_says_when_ffmpeg_is_missing(hass, players, monkeypatch):
    from homeassistant.helpers.translation import async_get_translations

    relay = await setup_relay(hass, players)
    entry = await hass.config_entries.flow.async_init("tts_relay", context={"source": "user"})
    form = await hass.config_entries.flow.async_configure(
        entry["flow_id"], {"next_step_id": "add_route"}
    )
    assert form["description_placeholders"]["ffmpeg"] == ""

    relay.set_ffmpeg(False)
    entry = await hass.config_entries.flow.async_init("tts_relay", context={"source": "user"})
    form = await hass.config_entries.flow.async_configure(
        entry["flow_id"], {"next_step_id": "add_route"}
    )
    assert "ffmpeg isn't available" in form["description_placeholders"]["ffmpeg"]

    texts = await async_get_translations(hass, "en", "config", ["tts_relay"])
    assert texts["component.tts_relay.config.step.add_route.description"].endswith("{ffmpeg}")
    texts = await async_get_translations(hass, "en", "issues", ["tts_relay"])
    assert "{routes}" in texts["component.tts_relay.issues.silence_needs_ffmpeg.description"]

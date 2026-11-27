# The fixtures from test_silence are imported, so tests taking them look like redefinitions.
# ruff: noqa: F811

import asyncio

import pytest
from conftest import BASE_URL, FakeSonos, setup_relay
from homeassistant.helpers import issue_registry as ir
from test_silence import (  # noqa: F401 - hass and tone_tts_integration are fixtures
    SAT,
    SPEAKER,
    decode,
    fetch,
    ffmpeg_issue,
    hass,
    no_ffmpeg,
    read,
    reply,
    start,
    tone,
    tone_tts_integration,
)

ROUTE = {"name": "Living room", "satellites": [SAT], "target": SPEAKER, "convert_to_wav": True}


def format_issue(hass):
    return ir.async_get(hass).async_get_issue("tts_relay", f"reply_format_{SAT}")


async def test_flac_replies_reach_sonos_as_wav(hass, players):
    players.sonos[SPEAKER] = FakeSonos()
    relay = await setup_relay(hass, players, [ROUTE])
    stream = reply(hass, "flac")
    await start(hass, relay, stream)

    url = players.urls()[-1]  # After the duck clip.
    assert url == f"{BASE_URL}/api/tts_relay/wav/{stream.token}.wav"
    assert format_issue(hass) is None
    status, audio = await fetch(hass, url)
    assert status == 200
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    seconds, loudest = await decode(audio)
    assert loudest > 10000
    assert seconds == pytest.approx(2.0, abs=0.1)


async def test_without_the_option_sonos_and_flac_raise_a_repair(hass, players):
    players.sonos[SPEAKER] = FakeSonos()
    relay = await setup_relay(hass, players, [{**ROUTE, "convert_to_wav": False}])
    stream = reply(hass, "flac")
    await start(hass, relay, stream)
    assert players.urls()[-1] == BASE_URL + stream.url
    assert format_issue(hass) is not None


@pytest.mark.parametrize("extension", ["wav", "mp3"])
async def test_formats_every_speaker_plays_stay_as_they_are(hass, players, extension):
    relay = await setup_relay(hass, players, [ROUTE])
    stream = reply(hass, extension)
    await start(hass, relay, stream)
    assert players.urls() == [BASE_URL + stream.url]


async def test_with_silence_the_satellite_is_silent_and_the_speaker_gets_wav(hass, players):
    relay = await setup_relay(hass, players, [{**ROUTE, "silence": True}])
    stream = reply(hass, "flac")
    await start(hass, relay, stream)
    [url] = players.urls()
    assert url.endswith(".wav")
    assert (await decode((await fetch(hass, url))[1]))[1] > 10000
    assert (await decode(await read(stream)))[1] == 0


async def test_a_reply_still_being_written_converts_as_it_comes(hass):
    from test_silence import _encode

    from custom_components.tts_relay.silence import wav_copy

    flac = await _encode(tone(10.0), "flac")
    more = asyncio.Event()

    async def generated():
        yield flac[: len(flac) // 2]
        await more.wait()
        yield flac[len(flac) // 2 :]

    copy = wav_copy(hass, generated(), "flac")
    first = await asyncio.wait_for(anext(copy), timeout=5)
    more.set()
    seconds, loudest = await decode(first + b"".join([chunk async for chunk in copy]))
    assert loudest > 10000
    assert seconds == pytest.approx(10.0, abs=0.1)


async def test_without_ffmpeg_replies_stay_flac_and_a_repair_says_why(hass, players, monkeypatch):
    no_ffmpeg(monkeypatch)
    relay = await setup_relay(hass, players, [ROUTE])
    assert ffmpeg_issue(hass).translation_placeholders == {"routes": "Living room"}
    stream = reply(hass, "flac")
    await start(hass, relay, stream)
    assert players.urls() == [BASE_URL + stream.url]


async def test_original_when_the_conversion_fails_after_all(hass, players, monkeypatch):
    relay = await setup_relay(hass, players, [ROUTE])
    stream = reply(hass, "flac")
    await start(hass, relay, stream)
    [url] = players.urls()
    no_ffmpeg(monkeypatch)  # E.g. uninstalled since.
    status, audio = await fetch(hass, url)
    assert status == 200
    assert audio[:4] == b"fLaC"
    assert ffmpeg_issue(hass) is not None

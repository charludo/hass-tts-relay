import sys
from pathlib import Path

import pytest
from conftest import setup_relay, start_hass
from homeassistant.components import tts
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.setup import async_setup_component

SAT = "assist_satellite.living_room"
SPEAKER = "media_player.living_room"
SILENCE = Path(__file__).parent.parent / "custom_components" / "tts_relay" / "silence.mp3"
CHIME = b"\xff\xfb" + b"chime" * 400
LONG_APOLOGY = "Sorry, I couldn't understand " + "and then something else " * 20

NO_MATCH = {"response_type": "error", "data": {"code": "no_intent_match"}}
FAILED = {"response_type": "error", "data": {"code": "failed_to_handle"}}


ENGINE = """
from homeassistant.components.tts import Provider


class Engine(Provider):
    name = "Test"
    default_language = "en"
    supported_languages = ["en"]

    def get_tts_audio(self, message, language, options):
        return "mp3", b"\\xff\\xfb" + message.encode()


async def async_get_engine(hass, config, discovery_info=None):
    return Engine()
"""


@pytest.fixture
def test_tts_integration(tmp_path):
    # Before Home Assistant starts, which is when it looks for custom integrations.
    integration = tmp_path / "custom_components" / "test_tts"
    integration.mkdir(parents=True)
    (integration / "manifest.json").write_text(
        '{"domain": "test_tts", "name": "Test TTS", "version": "1.0",'
        ' "documentation": "", "codeowners": [], "requirements": []}'
    )
    (integration / "tts.py").write_text(ENGINE)
    if (package := sys.modules.get("custom_components")) is not None:
        package.__path__.append(str(integration.parent))


@pytest.fixture
async def hass(test_tts_integration, tmp_path):
    hass = await start_hass(tmp_path)
    yield hass
    await hass.async_stop(force=True)


@pytest.fixture
async def speech(hass):
    """A reply stream, as the pipeline makes one, with the long speech set."""
    assert await async_setup_component(hass, "tts", {"tts": {"platform": "test_tts"}})
    await hass.async_block_till_done()
    stream = tts.async_create_stream(hass, "test_tts", "en")
    stream.async_set_message(LONG_APOLOGY)
    return stream


async def played_by_satellite(stream) -> bytes:
    return b"".join([chunk async for chunk in stream.async_stream_result()])


async def reply(hass, relay, stream, response):
    for event in (
        PipelineEvent(PipelineEventType.RUN_START, None),
        PipelineEvent(PipelineEventType.INTENT_END, {"intent_output": {"response": response}}),
        PipelineEvent(
            PipelineEventType.TTS_END,
            {"tts_output": {"url": stream.url + ".mp3", "token": stream.token}},
        ),
    ):
        relay.handle_event(SAT, event)
    await hass.async_block_till_done(wait_background_tasks=True)


def route(*rules):
    return [{"name": "Living room", "satellites": [SAT], "target": SPEAKER, "rules": list(rules)}]


def sound_file(hass, folder, name) -> str:
    path = Path(hass.config.path(*folder, name))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CHIME)
    return name


async def test_a_dropped_reply_is_silence_on_the_satellite(hass, players, speech):
    relay = await setup_relay(hass, players, route("quiet_on_no_match"))
    await reply(hass, relay, speech, NO_MATCH)
    assert players.played == []
    assert await played_by_satellite(speech) == SILENCE.read_bytes()


async def test_a_chime_plays_on_the_satellite_too(hass, players, speech):
    sound_file(hass, ("tts_relay", "sounds"), "error.mp3")
    relay = await setup_relay(hass, players, route("chime_on_error"))
    await reply(hass, relay, speech, FAILED)
    assert players.urls()[-1].endswith("/tts_relay/sounds/error.mp3")
    assert await played_by_satellite(speech) == CHIME


async def test_sounds_from_www_play_on_the_satellite_too(hass, players, speech):
    sound_file(hass, ("www", "sounds"), "error.mp3")
    relay = await setup_relay(
        hass, players, route("chime_on_error"), sounds={"error": "/local/sounds/error.mp3"}
    )
    await reply(hass, relay, speech, FAILED)
    assert await played_by_satellite(speech) == CHIME


async def test_remote_sounds_are_silence_on_the_satellite(hass, players, speech):
    relay = await setup_relay(
        hass, players, route("chime_on_error"), sounds={"error": "https://example.com/e.mp3"}
    )
    await reply(hass, relay, speech, FAILED)
    assert players.urls() == ["https://example.com/e.mp3"]
    assert await played_by_satellite(speech) == SILENCE.read_bytes()


async def test_no_files_outside_the_sound_folders(hass, players, speech):
    Path(hass.config.path("secrets.yaml")).write_text("password: hunter2\n")
    relay = await setup_relay(
        hass, players, route("chime_on_error"), sounds={"error": "/local/../secrets.yaml"}
    )
    await reply(hass, relay, speech, FAILED)
    assert await played_by_satellite(speech) == SILENCE.read_bytes()


async def test_speech_stays_speech(hass, players, speech):
    relay = await setup_relay(hass, players, route("quiet_on_no_match"))
    await reply(hass, relay, speech, {"response_type": "action_done", "data": {}})
    assert speech.async_get_media_path() != SILENCE
    assert await played_by_satellite(speech) not in (SILENCE.read_bytes(), b"")


async def test_the_satellite_gets_the_choice_before_it_plays(hass, players, speech):
    from test_relay import satellite_entity

    relay = await setup_relay(hass, players, route("quiet_on_no_match"))
    entity = satellite_entity(hass, SAT)
    seen = []
    entity.on_pipeline_event = lambda event: seen.append(speech.async_get_media_path())
    relay.handle_event(
        SAT,
        PipelineEvent(PipelineEventType.INTENT_END, {"intent_output": {"response": NO_MATCH}}),
    )
    entity._internal_on_pipeline_event(
        PipelineEvent(
            PipelineEventType.TTS_END,
            {"tts_output": {"url": speech.url + ".mp3", "token": speech.token}},
        )
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [path.resolve() for path in seen] == [SILENCE.resolve()]

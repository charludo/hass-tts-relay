from pathlib import Path

from conftest import BASE_URL, FakeSonos, setup_relay
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType

SAT = "assist_satellite.living_room"
SPEAKER = "media_player.living_room"
TTS = "/api/tts_proxy/stream.mp3"
ROUTE = {"name": "Living room", "satellites": [SAT], "target": SPEAKER, "stream": True}


def ev(type_, data=None):
    return PipelineEvent(type_, data)


def run_start(*, streams=True, url=TTS):
    return ev(
        PipelineEventType.RUN_START,
        {"tts_output": {"url": url, "token": "stream.mp3", "stream_response": streams}},
    )


WRITING = ev(PipelineEventType.INTENT_PROGRESS, {"chat_log_delta": {"content": "Well, "}})
STREAMING = ev(PipelineEventType.INTENT_PROGRESS, {"tts_start_streaming": True})
INTENT_END = ev(
    PipelineEventType.INTENT_END,
    {"intent_output": {"response": {"response_type": "action_done", "data": {"success": []}}}},
)
TTS_END = ev(PipelineEventType.TTS_END, {"tts_output": {"url": TTS}})
RUN_END = ev(PipelineEventType.RUN_END)


async def run(hass, relay, *events):
    for event in events:
        relay.handle_event(SAT, event)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_plays_once_the_agent_streams(hass, players):
    relay = await setup_relay(hass, players, [ROUTE])
    await run(hass, relay, run_start(), WRITING)
    assert players.urls() == []
    await run(hass, relay, STREAMING)
    assert players.urls() == [BASE_URL + TTS]
    # Not again when the agent is done.
    await run(hass, relay, INTENT_END, TTS_END, RUN_END)
    assert players.urls() == [BASE_URL + TTS]


async def test_waits_for_the_response_by_default(hass, players):
    relay = await setup_relay(hass, players, [{**ROUTE, "stream": False}])
    await run(hass, relay, run_start(), STREAMING)
    assert players.urls() == []
    await run(hass, relay, INTENT_END, TTS_END)
    assert players.urls() == [BASE_URL + TTS]


async def test_replies_that_dont_stream_play_as_usual(hass, players):
    relay = await setup_relay(hass, players, [ROUTE])
    for start in (run_start(streams=False), ev(PipelineEventType.RUN_START)):
        await run(hass, relay, start, INTENT_END, TTS_END)
    assert players.urls() == [BASE_URL + TTS] * 2


async def test_next_run_streams_again(hass, players):
    relay = await setup_relay(hass, players, [ROUTE])
    await run(hass, relay, run_start(), STREAMING, TTS_END)
    # The conversation goes on, and the answer streams too.
    await run(hass, relay, run_start(), STREAMING, TTS_END)
    assert players.urls() == [BASE_URL + TTS] * 2


async def test_ducking_ends_when_the_reply_starts(hass, players):
    sonos = players.sonos[SPEAKER] = FakeSonos()
    relay = await setup_relay(hass, players, [ROUTE])
    await run(hass, relay, run_start())
    duck_clip = sonos.last_announce_id
    await run(hass, relay, STREAMING)
    assert sonos.websocket.cancelled == [duck_clip]
    assert players.urls()[-1] == BASE_URL + TTS


async def test_a_rule_that_decides_without_the_response_waits_for_it(hass, players):
    relay = await setup_relay(hass, players, [{**ROUTE, "rules": ["night"]}])
    Path(hass.config.path("tts_relay", "rules", "night.yaml")).write_text(
        "name: Night\ndescription: Nothing at night.\n"
        "reply: \"{{ 'drop' if is_state('input_boolean.night', 'on') }}\"\n"
    )
    await hass.services.async_call("tts_relay", "reload", blocking=True)
    hass.states.async_set("input_boolean.night", "on")
    await run(hass, relay, run_start(), STREAMING, INTENT_END, TTS_END)
    assert players.urls() == []

    hass.states.async_set("input_boolean.night", "off")
    await run(hass, relay, run_start(), STREAMING)
    assert players.urls() == [BASE_URL + TTS]


async def test_a_streamed_flac_reply_is_flagged(hass, players):
    from homeassistant.helpers import issue_registry as ir

    players.sonos[SPEAKER] = FakeSonos()
    relay = await setup_relay(hass, players, [ROUTE])
    await run(hass, relay, run_start(url="/api/tts_proxy/stream.flac"), STREAMING)
    assert ir.async_get(hass).async_get_issue("tts_relay", f"reply_format_{SAT}") is not None

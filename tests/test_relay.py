import aiohttp
from conftest import BASE_URL, FakeSonos, setup_relay
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType

SAT = "assist_satellite.living_room"
OTHER_SAT = "assist_satellite.kitchen"
SPEAKER = "media_player.living_room"
TTS = "/api/tts_proxy/abc.mp3"


def ev(type_, data=None):
    return PipelineEvent(type_, data)


def intent_end(response: dict) -> PipelineEvent:
    return ev(PipelineEventType.INTENT_END, {"intent_output": {"response": response}})


def tts_end(url: str | None = TTS) -> PipelineEvent:
    return ev(PipelineEventType.TTS_END, {"tts_output": {"url": url} if url else None})


async def run(hass, relay, *events, satellite=SAT):
    for event in events:
        relay.handle_event(satellite, event)
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_relays_tts_as_absolute_announcement(hass, players):
    relay = await setup_relay(
        hass, players, [{"name": "Living room", "satellites": [SAT], "target": SPEAKER}]
    )
    await run(
        hass,
        relay,
        ev(PipelineEventType.RUN_START),
        ev(PipelineEventType.STT_VAD_END),
        intent_end({"speech": {"plain": {"speech": "Done"}}}),
        tts_end(),
        ev(PipelineEventType.RUN_END),
    )
    assert players.played == [
        {
            "entity_id": SPEAKER,
            "announce": True,
            "media_content_id": BASE_URL + TTS,
            "media_content_type": "music",
        }
    ]


async def test_ignores_unrouted_satellites(hass, players):
    relay = await setup_relay(
        hass, players, [{"name": "Living room", "satellites": [SAT], "target": SPEAKER}]
    )
    await run(hass, relay, ev(PipelineEventType.RUN_START), tts_end(), satellite=OTHER_SAT)
    assert players.played == []


async def test_sonos_ducks_while_listening(hass, players):
    sonos = players.sonos[SPEAKER] = FakeSonos()
    relay = await setup_relay(
        hass,
        players,
        [{"name": "Living room", "satellites": [SAT], "target": SPEAKER, "volume": 0.35}],
    )

    await run(hass, relay, ev(PipelineEventType.RUN_START))
    assert players.urls() == [BASE_URL + "/tts_relay/duck.mp3"]
    assert players.played[0]["extra"] == {"volume": 35}
    duck_clip = sonos.last_announce_id

    await run(hass, relay, ev(PipelineEventType.STT_VAD_END))
    assert sonos.websocket.cancelled == [duck_clip]

    await run(hass, relay, intent_end({}), tts_end(), ev(PipelineEventType.RUN_END))
    assert players.urls()[1:] == [BASE_URL + TTS]
    assert sonos.websocket.cancelled == [duck_clip]


async def test_duck_cancelled_before_reply_without_vad_end(hass, players):
    sonos = players.sonos[SPEAKER] = FakeSonos()
    relay = await setup_relay(
        hass, players, [{"name": "Living room", "satellites": [SAT], "target": SPEAKER}]
    )
    await run(hass, relay, ev(PipelineEventType.RUN_START))
    duck_clip = sonos.last_announce_id
    await run(hass, relay, tts_end())
    assert sonos.websocket.cancelled == [duck_clip]
    assert players.urls()[-1] == BASE_URL + TTS


async def test_aborted_run_cancels_duck(hass, players):
    sonos = players.sonos[SPEAKER] = FakeSonos()
    relay = await setup_relay(
        hass, players, [{"name": "Living room", "satellites": [SAT], "target": SPEAKER}]
    )
    await run(
        hass,
        relay,
        ev(PipelineEventType.RUN_START),
        ev(PipelineEventType.ERROR, {"code": "stt-no-text-recognized"}),
    )
    assert sonos.websocket.cancelled == [sonos.last_announce_id]


async def test_duck_can_be_turned_off(hass, players):
    players.sonos[SPEAKER] = FakeSonos()
    relay = await setup_relay(
        hass,
        players,
        [{"name": "Living room", "satellites": [SAT], "target": SPEAKER, "duck": False}],
    )
    await run(hass, relay, ev(PipelineEventType.RUN_START), tts_end())
    assert players.urls() == [BASE_URL + TTS]


async def test_wraps_the_satellite_entity(hass, players):
    from homeassistant.components.assist_satellite.entity import AssistSatelliteEntity

    await setup_relay(
        hass, players, [{"name": "Living room", "satellites": [SAT], "target": SPEAKER}]
    )

    class Satellite(AssistSatelliteEntity):
        def on_pipeline_event(self, event):
            self.events.append(event.type)

        def async_get_configuration(self):
            raise NotImplementedError

        async def async_set_configuration(self, config):
            raise NotImplementedError

    satellite = Satellite()
    satellite.hass = hass
    satellite.entity_id = SAT
    satellite.events = []
    satellite._internal_on_pipeline_event(tts_end())
    await hass.async_block_till_done(wait_background_tasks=True)

    assert satellite.events == [PipelineEventType.TTS_END]
    assert players.urls() == [BASE_URL + TTS]


async def test_stop(hass, players):
    sonos = players.sonos[SPEAKER] = FakeSonos()
    sonos.last_announce_id = "clip-playing"
    await setup_relay(hass, players)
    await hass.services.async_call(
        "tts_relay", "stop", {"entity_id": [SPEAKER, "media_player.other"]}, blocking=True
    )
    assert players.stopped == [[SPEAKER, "media_player.other"]]
    assert sonos.websocket.cancelled == ["clip-playing"]


async def test_serves_duck_clip(hass, players):
    await setup_relay(hass, players)
    await hass.async_start()
    url = f"http://127.0.0.1:{hass.data['test_port']}/tts_relay/duck.mp3"
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        assert response.status == 200
        assert len(await response.read()) > 1000


BOTH = {"name": "Living room", "satellites": [SAT, OTHER_SAT], "target": SPEAKER}
DUCK = BASE_URL + "/tts_relay/duck.mp3"


async def test_shared_sonos_keeps_one_duck_clip(hass, players):
    sonos = players.sonos[SPEAKER] = FakeSonos()
    relay = await setup_relay(hass, players, [BOTH])
    await run(hass, relay, ev(PipelineEventType.RUN_START))
    duck_clip = sonos.last_announce_id
    await run(hass, relay, ev(PipelineEventType.RUN_START), satellite=OTHER_SAT)
    assert players.urls() == [DUCK]

    await run(hass, relay, ev(PipelineEventType.STT_VAD_END))
    assert sonos.websocket.cancelled == []
    await run(hass, relay, ev(PipelineEventType.RUN_END), satellite=OTHER_SAT)
    assert sonos.websocket.cancelled == [duck_clip]


async def test_sonos_ducks_again_after_a_reply_while_another_listens(hass, players):
    sonos = players.sonos[SPEAKER] = FakeSonos()
    relay = await setup_relay(hass, players, [BOTH])
    await run(hass, relay, ev(PipelineEventType.RUN_START))
    first_clip = sonos.last_announce_id
    await run(hass, relay, ev(PipelineEventType.RUN_START), satellite=OTHER_SAT)

    await run(hass, relay, tts_end())
    assert sonos.websocket.cancelled == [first_clip]
    assert players.urls() == [DUCK, BASE_URL + TTS, DUCK]

    await run(hass, relay, ev(PipelineEventType.STT_VAD_END), satellite=OTHER_SAT)
    assert sonos.websocket.cancelled == [first_clip, sonos.last_announce_id]


async def test_yaml_routes_must_be_distinct(hass):
    import pytest
    import voluptuous as vol

    from custom_components.tts_relay import CONFIG_SCHEMA

    office = {"name": "Office", "satellites": "assist_satellite.office", "target": SPEAKER}
    conf = CONFIG_SCHEMA({"tts_relay": {"routes": [office]}})
    assert conf["tts_relay"]["routes"][0]["satellites"] == ["assist_satellite.office"]
    for routes in (
        [BOTH, {**BOTH, "name": "Living Room", "satellites": ["assist_satellite.office"]}],
        [BOTH, {**BOTH, "name": "Other", "satellites": [SAT]}],
    ):
        with pytest.raises(vol.Invalid):
            CONFIG_SCHEMA({"tts_relay": {"routes": routes}})

import aiohttp
from conftest import BASE_URL, FakeSonos, setup_relay
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.data_entry_flow import FlowResultType

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

    del players.sonos[SPEAKER]
    playing(hass)
    await run(hass, relay, ev(PipelineEventType.RUN_START), tts_end())
    assert players.volumes == []


def playing(hass, volume=0.5, state="playing", features=4):
    hass.states.async_set(SPEAKER, state, {"volume_level": volume, "supported_features": features})


async def test_other_players_duck_by_volume(hass, players):
    playing(hass)
    relay = await setup_relay(
        hass, players, [{"name": "Living room", "satellites": [SAT], "target": SPEAKER}]
    )
    await run(hass, relay, ev(PipelineEventType.RUN_START))
    assert players.volumes == [(SPEAKER, 0.15)]
    assert players.played == []

    await run(hass, relay, ev(PipelineEventType.STT_VAD_END))
    assert players.volumes == [(SPEAKER, 0.15), (SPEAKER, 0.5)]

    await run(hass, relay, tts_end(), ev(PipelineEventType.RUN_END))
    assert len(players.volumes) == 2
    assert players.urls() == [BASE_URL + TTS]


async def test_volume_restored_before_the_reply(hass, players):
    playing(hass)
    relay = await setup_relay(
        hass, players, [{"name": "Living room", "satellites": [SAT], "target": SPEAKER}]
    )
    await run(hass, relay, ev(PipelineEventType.RUN_START), tts_end())
    assert players.volumes == [(SPEAKER, 0.15), (SPEAKER, 0.5)]


async def test_no_volume_duck_when_idle_or_unsupported(hass, players):
    relay = await setup_relay(
        hass, players, [{"name": "Living room", "satellites": [SAT], "target": SPEAKER}]
    )
    for kwargs in ({"state": "idle"}, {"features": 0}):
        playing(hass, **kwargs)
        await run(hass, relay, ev(PipelineEventType.RUN_START), ev(PipelineEventType.RUN_END))
    assert players.volumes == []


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


async def test_shared_speaker_stays_ducked_until_the_last_satellite_is_done(hass, players):
    playing(hass)
    relay = await setup_relay(hass, players, [BOTH])
    await run(hass, relay, ev(PipelineEventType.RUN_START))
    await run(hass, relay, ev(PipelineEventType.RUN_START), satellite=OTHER_SAT)
    assert players.volumes == [(SPEAKER, 0.15)]

    await run(hass, relay, ev(PipelineEventType.STT_VAD_END))
    assert players.volumes == [(SPEAKER, 0.15)]
    await run(hass, relay, ev(PipelineEventType.STT_VAD_END), satellite=OTHER_SAT)
    assert players.volumes == [(SPEAKER, 0.15), (SPEAKER, 0.5)]


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


async def start_setup(hass, choice):
    result = await hass.config_entries.flow.async_init("tts_relay", context={"source": "user"})
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": choice}
    )


async def settle(hass):
    await hass.async_block_till_done(wait_background_tasks=True)


def entry(hass):
    (only,) = hass.config_entries.async_entries("tts_relay")
    return only


def subentry(hass, id_):
    (sub,) = [s for s in entry(hass).subentries.values() if s.unique_id == id_]
    return sub


async def add_route(hass, form):
    result = await hass.config_entries.subentries.async_init(
        (entry(hass).entry_id, "route"), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], form)
    await settle(hass)
    return result


async def reconfigure(hass, id_, form=None):
    result = await hass.config_entries.subentries.async_init(
        (entry(hass).entry_id, "route"),
        context={"source": "reconfigure", "subentry_id": subentry(hass, id_).subentry_id},
    )
    if form is not None:
        result = await hass.config_entries.subentries.async_configure(result["flow_id"], form)
        await settle(hass)
    return result


def add_subentry(hass, id_, data):
    from types import MappingProxyType

    from homeassistant.config_entries import ConfigSubentry

    hass.config_entries.async_add_subentry(
        entry(hass),
        ConfigSubentry(
            data=MappingProxyType(data), subentry_type="route", title=data["name"], unique_id=id_
        ),
    )


async def test_setup_with_a_first_route(hass, players):
    relay = await setup_relay(hass, players)

    result = await start_setup(hass, "add_route")
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BOTH, "volume": 40}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await settle(hass)
    route = relay.route_for(OTHER_SAT)
    assert route is relay.route_for(SAT)
    assert (route.name, route.target, route.volume, route.duck) == (
        "Living room",
        SPEAKER,
        0.4,
        True,
    )
    assert subentry(hass, "living_room").title == "Living room"

    result = await hass.config_entries.flow.async_init("tts_relay", context={"source": "user"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_setup_without_routes_then_add_change_remove(hass, players):
    relay = await setup_relay(hass, players)

    result = await start_setup(hass, "finish")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await settle(hass)
    assert not entry(hass).subentries
    assert not relay.routes

    result = await add_route(hass, BOTH)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert relay.route_for(OTHER_SAT).target == SPEAKER

    office = "media_player.office"
    result = await add_route(
        hass, {"name": "Living Room", "satellites": ["assist_satellite.x"], "target": office}
    )
    assert result["errors"] == {"name": "name_taken"}
    result = await add_route(hass, {"name": "Office", "satellites": [SAT], "target": office})
    assert result["errors"] == {"satellites": "satellite_taken"}
    assert result["description_placeholders"]["taken"] == f"{SAT} (Living room)"

    result = await reconfigure(hass, "living_room", {**BOTH, "duck": False})
    assert result["reason"] == "reconfigure_successful"
    assert relay.route_for(SAT).duck is False

    result = await reconfigure(
        hass, "living_room", {"name": "Downstairs", "satellites": [SAT], "target": office}
    )
    assert result["reason"] == "reconfigure_successful"
    route = relay.route_for(SAT)
    assert (route.name, route.target, route.duck) == ("Downstairs", office, False)
    assert relay.route_for(OTHER_SAT) is None
    assert subentry(hass, "downstairs").title == "Downstairs"

    hass.config_entries.async_remove_subentry(entry(hass), subentry(hass, "downstairs").subentry_id)
    await settle(hass)
    assert relay.route_for(SAT) is None


async def test_yaml_routes_are_listed_but_not_editable(hass, players):
    relay = await setup_relay(hass, players, [{**BOTH, "satellites": [SAT], "volume": 0.3}])
    result = await start_setup(hass, "add_route")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Other", "satellites": [SAT, OTHER_SAT], "target": SPEAKER}
    )
    assert result["errors"] == {"satellites": "satellite_taken"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Living room", "satellites": [OTHER_SAT], "target": SPEAKER}
    )
    assert result["errors"] == {"name": "name_taken"}

    await start_setup(hass, "finish")
    await settle(hass)
    mirror = subentry(hass, "living_room")
    assert mirror.title == "Living room (YAML)"
    assert (mirror.data["satellites"], mirror.data["volume"]) == ([SAT], 0.3)

    result = await reconfigure(hass, "living_room")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "managed_in_yaml"

    result = await add_route(hass, {"name": "Kitchen", "satellites": [SAT], "target": SPEAKER})
    assert result["errors"] == {"satellites": "satellite_taken"}

    hass.config_entries.async_remove_subentry(entry(hass), mirror.subentry_id)
    await settle(hass)
    assert relay.route_for(SAT).source == "yaml"
    assert subentry(hass, "living_room").data["yaml"] is True


async def test_mirrors_follow_the_yaml(hass, players):
    relay = await setup_relay(hass, players, [BOTH])
    await start_setup(hass, "finish")
    await settle(hass)
    gone = {"name": "Gone", "satellites": ["assist_satellite.x"], "target": SPEAKER, "yaml": True}
    add_subentry(hass, "gone", gone)
    await settle(hass)
    assert [s.unique_id for s in entry(hass).subentries.values()] == ["living_room"]
    assert relay.route_for("assist_satellite.x") is None


async def test_yaml_takes_over_a_ui_route(hass, players):
    relay = await setup_relay(hass, players, [{**BOTH, "satellites": [SAT]}])
    await start_setup(hass, "finish")
    await settle(hass)
    hass.config_entries.async_update_subentry(
        entry(hass), subentry(hass, "living_room"), data={**BOTH, "target": "media_player.office"}
    )
    await settle(hass)
    assert relay.route_for(SAT).target == SPEAKER
    assert subentry(hass, "living_room").data["yaml"] is True

    add_subentry(
        hass, "kitchen", {"name": "Kitchen", "satellites": [SAT, OTHER_SAT], "target": SPEAKER}
    )
    await settle(hass)
    assert relay.route_for(SAT).source == "yaml"
    assert relay.route_for(OTHER_SAT).name == "Kitchen"


async def test_ui_texts_load(hass, players):
    from homeassistant.helpers.translation import async_get_translations

    await setup_relay(hass, players)
    texts = await async_get_translations(hass, "en", "config", ["tts_relay"])
    assert texts["component.tts_relay.config.step.add_route.data.satellites"] == "Satellites"
    texts = await async_get_translations(hass, "en", "config_subentries", ["tts_relay"])
    assert texts["component.tts_relay.config_subentries.route.initiate_flow.user"] == "Add route"

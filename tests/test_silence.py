"""Muting the satellites' own speakers while they play a reply we relay."""

import json
from types import MappingProxyType

import pytest
from conftest import setup_relay, start_hass
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import ServiceCall
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

SAT = "assist_satellite.living_room"
SPEAKER = "media_player.living_room"
OWN = "media_player.voice_pe"  # the satellite's own media player
MUTE = 8  # MediaPlayerEntityFeature.VOLUME_MUTE
ROUTE = {"name": "Living room", "satellites": [SAT], "target": SPEAKER, "silence": True}


def register(hass, *, own=OWN):
    # Like Home Assistant's own MockConfigEntry.add_to_hass.
    config_entry = ConfigEntry(
        data={},
        discovery_keys=MappingProxyType({}),
        domain="esphome",
        minor_version=1,
        options={},
        source="user",
        subentries_data=None,
        title="Voice PE",
        unique_id="voice-pe",
        version=1,
    )
    hass.config_entries._entries[config_entry.entry_id] = config_entry
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("esphome", "voice-pe")}
    )
    registry = er.async_get(hass)
    for domain, unique_id, object_id in (
        ("assist_satellite", "sat", "living_room"),
        ("media_player", "own", own.split(".")[1]),
    ):
        entry = registry.async_get_or_create(
            domain, "esphome", unique_id, suggested_object_id=object_id
        )
        registry.async_update_entity(entry.entity_id, device_id=device.id)


def own_player(hass, *, muted=False, state="idle", features=MUTE, entity_id=OWN):
    hass.states.async_set(
        entity_id, state, {"is_volume_muted": muted, "supported_features": features}
    )


@pytest.fixture
def mutes(hass):
    calls = []

    async def volume_mute(call: ServiceCall) -> None:
        muted = call.data["is_volume_muted"]
        for entity_id in call.data["entity_id"]:
            calls.append((entity_id, muted))
            state = hass.states.get(entity_id)
            hass.states.async_set(
                entity_id, state.state, {**state.attributes, "is_volume_muted": muted}
            )

    hass.services.async_register("media_player", "volume_mute", volume_mute)
    return calls


async def satellite(hass, *states):
    for state in states:
        hass.states.async_set(SAT, state)
        await hass.async_block_till_done(wait_background_tasks=True)


async def start(hass, relay):
    hass.states.async_set(SAT, "listening")
    relay.handle_event(SAT, PipelineEvent(PipelineEventType.RUN_START, None))
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_mutes_the_satellite_until_its_reply_ends(hass, players, mutes):
    register(hass)
    own_player(hass)
    relay = await setup_relay(hass, players, [ROUTE])

    await start(hass, relay)
    assert mutes == [(OWN, True)]
    assert await relay.silencer._store.async_load() == [OWN]

    await satellite(hass, "processing", "responding")
    assert mutes == [(OWN, True)]
    await satellite(hass, "idle")
    assert mutes == [(OWN, True), (OWN, False)]
    assert await relay.silencer._store.async_load() == []


async def test_stays_muted_for_a_whole_conversation(hass, players, mutes):
    register(hass)
    own_player(hass)
    relay = await setup_relay(hass, players, [ROUTE])
    await start(hass, relay)
    await satellite(hass, "responding")
    await start(hass, relay)  # It listens again, for the answer.
    await satellite(hass, "responding", "idle")
    assert mutes == [(OWN, True), (OWN, False)]


async def test_leaves_players_alone_that_were_muted_or_cant_be(hass, players, mutes):
    register(hass)
    relay = await setup_relay(hass, players, [ROUTE])
    for kwargs in ({"muted": True}, {"features": 0}, {"state": "unavailable"}):
        own_player(hass, **kwargs)
        await start(hass, relay)
        await satellite(hass, "idle")
    assert mutes == []


async def test_only_when_turned_on(hass, players, mutes):
    register(hass)
    own_player(hass)
    relay = await setup_relay(hass, players, [{**ROUTE, "silence": False}])
    await start(hass, relay)
    assert mutes == []


async def test_never_mutes_the_speaker_replies_go_to(hass, players, mutes):
    register(hass, own=SPEAKER)
    own_player(hass, entity_id=SPEAKER)
    relay = await setup_relay(hass, players, [ROUTE])
    await start(hass, relay)
    assert mutes == []


async def test_unmutes_when_the_satellite_goes_away(hass, players, mutes):
    register(hass)
    own_player(hass)
    relay = await setup_relay(hass, players, [ROUTE])
    await start(hass, relay)
    await satellite(hass, "unavailable")
    assert mutes[-1] == (OWN, False)


async def test_mutes_before_an_announcement_plays(hass, players, mutes):
    from test_relay import satellite_entity

    register(hass)
    own_player(hass)
    await setup_relay(hass, players, [ROUTE])
    entity = satellite_entity(hass, SAT)
    hass.states.async_set(SAT, "responding")
    # Muted by the time the satellite gets the announcement to play.
    await entity._resolve_announcement_media_id("Dinner", "/local/dinner.mp3")
    assert mutes == [(OWN, True)]
    await satellite(hass, "idle")
    assert mutes == [(OWN, True), (OWN, False)]


async def test_announcements_left_on_the_satellite_stay_audible(hass, players, mutes):
    from test_relay import satellite_entity

    register(hass)
    own_player(hass)
    await setup_relay(hass, players, [{**ROUTE, "announcements": False}])
    await satellite_entity(hass, SAT)._resolve_announcement_media_id("Hi", "/local/hi.mp3")
    assert mutes == []


@pytest.fixture
async def restarted_hass(tmp_path):
    # Home Assistant went down in the middle of a reply.
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / "tts_relay.muted").write_text(
        json.dumps({"version": 1, "minor_version": 1, "key": "tts_relay.muted", "data": [OWN]})
    )
    hass = await start_hass(tmp_path)
    yield hass
    await hass.async_stop(force=True)


@pytest.mark.parametrize("available_at_start", [True, False])
async def test_unmutes_after_a_restart_mid_reply(restarted_hass, available_at_start):
    hass = restarted_hass
    from conftest import Players

    calls = []

    async def volume_mute(call: ServiceCall) -> None:
        calls.append((call.data["entity_id"], call.data["is_volume_muted"]))

    hass.services.async_register("media_player", "volume_mute", volume_mute)
    if available_at_start:
        own_player(hass, muted=True)
    players = Players(hass)
    players.patch_sonos = lambda: None
    relay = await setup_relay(hass, players, [ROUTE])
    if not available_at_start:
        assert calls == []
        own_player(hass, muted=True)  # ESPHome connects
        await hass.async_block_till_done(wait_background_tasks=True)
    assert calls == [([OWN], False)]
    assert await relay.silencer._store.async_load() == []

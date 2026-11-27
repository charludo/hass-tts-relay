"""Volume changes while a (non-Sonos) speaker is ducked, against a real media player."""

import sys

import pytest
from conftest import start_hass
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType
from homeassistant.helpers import intent
from homeassistant.setup import async_setup_component

SAT = "assist_satellite.living_room"
OTHER_SAT = "assist_satellite.kitchen"
SPEAKER = "media_player.living_room"

PLATFORM = """
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)


class Player(MediaPlayerEntity):
    _attr_name = "Living room"
    _attr_should_poll = False
    _attr_state = MediaPlayerState.PLAYING
    _attr_supported_features = (
        MediaPlayerEntityFeature.VOLUME_SET | MediaPlayerEntityFeature.VOLUME_STEP
    )
    _attr_volume_level = 0.5

    def __init__(self):
        self.entity_id = "media_player.living_room"
        self.calls = []
        self.reports = True  # False: like a player that's slow to report

    async def async_set_volume_level(self, volume):
        self.calls.append(round(volume, 2))
        if self.reports:
            self.report(volume)

    def report(self, volume):
        self._attr_volume_level = volume
        self.async_write_ha_state()


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    hass.data["test_player"] = Player()
    async_add_entities([hass.data["test_player"]])
"""


@pytest.fixture
def test_player_integration(tmp_path):
    # Before Home Assistant starts, which is when it looks for custom integrations.
    integration = tmp_path / "custom_components" / "test_player"
    integration.mkdir(parents=True)
    (integration / "manifest.json").write_text(
        '{"domain": "test_player", "name": "Test player", "version": "1.0",'
        ' "documentation": "", "codeowners": [], "requirements": []}'
    )
    (integration / "media_player.py").write_text(PLATFORM)
    if (package := sys.modules.get("custom_components")) is not None:
        package.__path__.append(str(integration.parent))


@pytest.fixture
async def hass(test_player_integration, tmp_path):
    hass = await start_hass(tmp_path)
    yield hass
    await hass.async_stop(force=True)


@pytest.fixture
async def player(hass):
    assert await async_setup_component(
        hass, "media_player", {"media_player": [{"platform": "test_player"}]}
    )
    assert await async_setup_component(hass, "intent", {})
    await hass.async_block_till_done()
    return hass.data["test_player"]


@pytest.fixture
async def relay(hass, player):
    routes = [{"name": "Living room", "satellites": [SAT, OTHER_SAT], "target": SPEAKER}]
    assert await async_setup_component(hass, "tts_relay", {"tts_relay": {"routes": routes}})
    await hass.async_block_till_done()
    return hass.data["tts_relay"]


async def run(hass, relay, *types, satellite=SAT):
    for type_ in types:
        relay.handle_event(satellite, PipelineEvent(type_, None))
        await hass.async_block_till_done(wait_background_tasks=True)


async def say(hass, intent_type, **slots):
    await intent.async_handle(
        hass, "test", intent_type, {name: {"value": value} for name, value in slots.items()}
    )
    # Not waiting for background tasks, which includes waiting for a slow player.
    await hass.async_block_till_done()


def volume(hass):
    return round(hass.states.get(SPEAKER).attributes["volume_level"], 2)


DUCK = PipelineEventType.RUN_START
UNDUCK = PipelineEventType.STT_END


@pytest.mark.parametrize(
    ("intent_type", "slots", "restored"),
    [
        ("HassSetVolumeRelative", {"volume_step": "down"}, 0.4),
        ("HassSetVolumeRelative", {"volume_step": "up"}, 0.6),
        ("HassSetVolumeRelative", {"volume_step": 20}, 0.7),
        ("HassSetVolumeRelative", {"volume_step": -20}, 0.3),
        ("HassSetVolume", {"volume_level": 30}, 0.3),
    ],
)
async def test_changes_while_ducked_apply_after(hass, player, relay, intent_type, slots, restored):
    await run(hass, relay, DUCK, satellite=OTHER_SAT)
    assert player.calls == [0.15]

    # Another satellite on the same speaker, while the first still listens.
    await say(hass, intent_type, name="Living room", **slots)
    assert player.calls == [0.15]

    await run(hass, relay, UNDUCK, satellite=OTHER_SAT)
    assert player.calls == [0.15, restored]
    assert volume(hass) == restored


async def test_app_and_automations_while_ducked_apply_after(hass, player, relay):
    await run(hass, relay, DUCK)
    for level in (0.8, 0.2):
        await hass.services.async_call(
            "media_player",
            "volume_set",
            {"entity_id": SPEAKER, "volume_level": level},
            blocking=True,
        )
    await hass.services.async_call(
        "media_player", "volume_up", {"entity_id": SPEAKER}, blocking=True
    )
    assert player.calls == [0.15]
    await run(hass, relay, UNDUCK)
    assert player.calls == [0.15, 0.3]


async def test_quieter_before_a_slow_player_reports_the_restore(hass, player, relay):
    await run(hass, relay, DUCK)
    player.reports = False
    relay.handle_event(SAT, PipelineEvent(UNDUCK, None))
    await hass.async_block_till_done()
    assert player.calls == [0.15, 0.5]
    assert volume(hass) == 0.15  # not reported yet

    # Without the hold, this would step down from 0.15.
    await say(hass, "HassSetVolumeRelative", name="Living room", volume_step="down")
    assert player.calls == [0.15, 0.5, 0.4]

    player.report(0.4)
    await hass.async_block_till_done(wait_background_tasks=True)
    await say(hass, "HassSetVolumeRelative", name="Living room", volume_step="down")
    assert player.calls[-1] == 0.3  # the player's own now


async def test_hold_ends_when_a_player_never_reports(hass, player, relay, monkeypatch):
    monkeypatch.setattr("custom_components.tts_relay.volume.RESTORE_TIMEOUT", 0.05)
    await run(hass, relay, DUCK)
    player.reports = False
    await run(hass, relay, UNDUCK)
    assert "async_set_volume_level" not in vars(player)


async def test_ducking_again_before_the_report_keeps_the_volume(hass, player, relay, monkeypatch):
    # The first restore's wait runs out in the background.
    monkeypatch.setattr("custom_components.tts_relay.volume.RESTORE_TIMEOUT", 0.05)
    await run(hass, relay, DUCK)
    player.reports = False
    relay.handle_event(SAT, PipelineEvent(UNDUCK, None))
    await hass.async_block_till_done()
    # The player still says 0.15, which mustn't become the volume to restore to.
    await run(hass, relay, DUCK)
    player.reports = True
    await run(hass, relay, UNDUCK)
    assert player.calls == [0.15, 0.5, 0.15, 0.5]


async def test_reply_doesnt_wait_for_the_report(hass, player, relay, monkeypatch):
    plays = []

    async def announce(route, url):
        plays.append(player.calls[:])

    monkeypatch.setattr(relay, "announce", announce)
    await run(hass, relay, DUCK)
    player.reports = False
    relay.handle_event(
        SAT,
        PipelineEvent(PipelineEventType.TTS_END, {"tts_output": {"url": "/api/tts_proxy/a.mp3"}}),
    )
    await hass.async_block_till_done()
    assert plays == [[0.15, 0.5]]
    player.report(0.5)
    await hass.async_block_till_done(wait_background_tasks=True)

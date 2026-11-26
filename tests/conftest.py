import itertools
import socket
from pathlib import Path

import pytest
from homeassistant import block_async_io, loader
from homeassistant.auth import auth_manager_from_config
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "tts_relay"
BASE_URL = "http://ha.test:8123"

block_async_io._IN_TESTS = False
block_async_io.enable()


@pytest.fixture(autouse=True)
def no_blocking_calls_in_the_loop(caplog):
    yield
    blocking = [
        message
        for phase in ("setup", "call")
        for record in caplog.get_records(phase)
        if "Detected blocking call" in (message := record.getMessage())
        and "integration 'tts_relay'" in message
    ]
    assert not blocking, blocking[0]


async def start_hass(config_dir: Path) -> HomeAssistant:
    custom = config_dir / "custom_components"
    custom.mkdir(exist_ok=True)
    (custom / "tts_relay").symlink_to(PKG_DIR)
    hass = HomeAssistant(str(config_dir))
    hass.config.skip_pip = True
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    assert await async_load_base_functionality(hass)
    hass.auth = await auth_manager_from_config(hass, [], [])
    assert await async_setup_component(hass, "homeassistant", {})
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        hass.data["test_port"] = sock.getsockname()[1]
    assert await async_setup_component(
        hass,
        "http",
        {"http": {"server_host": "127.0.0.1", "server_port": hass.data["test_port"]}},
    )
    await hass.config.async_update(internal_url=BASE_URL)
    return hass


@pytest.fixture
async def hass(tmp_path):
    hass = await start_hass(tmp_path)
    yield hass
    await hass.async_stop(force=True)


class Players:
    def __init__(self, hass: HomeAssistant) -> None:
        self.played: list[dict] = []
        self.stopped: list[list[str]] = []
        self.volumes: list[tuple[str, float]] = []
        self.sonos: dict[str, FakeSonos] = {}

        async def play_media(call: ServiceCall) -> None:
            data = dict(call.data)
            self.played.append(data)
            ids = data["entity_id"]
            for entity_id in [ids] if isinstance(ids, str) else ids:
                if entity_id in self.sonos and data.get("announce"):
                    self.sonos[entity_id].started_clip()

        async def media_stop(call: ServiceCall) -> None:
            self.stopped.append(list(call.data["entity_id"]))

        hass.services.async_register("media_player", "play_media", play_media)

        async def volume_set(call: ServiceCall) -> None:
            self.volumes.append((call.data["entity_id"], round(call.data["volume_level"], 2)))

        hass.services.async_register("media_player", "media_stop", media_stop)
        hass.services.async_register("media_player", "volume_set", volume_set)

    def urls(self) -> list[str]:
        return [p["media_content_id"] for p in self.played]


class FakeWebsocket:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def cancel_clip(self, clip_id: str):
        self.cancelled.append(clip_id)
        return {"success": True}, {}


class FakeSonos:
    _ids = itertools.count(1)

    def __init__(self) -> None:
        self.websocket = FakeWebsocket()
        self.last_announce_id: str | None = None

    def started_clip(self) -> None:
        self.last_announce_id = f"clip-{next(self._ids)}"


@pytest.fixture
def players(hass, monkeypatch):
    players = Players(hass)

    def speaker(_hass, entity_id):
        return players.sonos.get(entity_id)

    def patch_sonos() -> None:
        monkeypatch.setattr("custom_components.tts_relay.sonos.speaker", speaker)

    players.patch_sonos = patch_sonos
    return players


async def setup_relay(
    hass: HomeAssistant,
    players: Players,
    routes: list[dict] | None = None,
    sounds: dict[str, str] | None = None,
):
    config = {"tts_relay": {"routes": routes or [], "sounds": sounds or {}}}
    assert await async_setup_component(hass, "tts_relay", config)
    await hass.async_block_till_done(wait_background_tasks=True)
    players.patch_sonos()
    from custom_components.tts_relay.const import DATA_RELAY

    return hass.data[DATA_RELAY]

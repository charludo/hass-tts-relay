from pathlib import Path

import aiohttp
from conftest import BASE_URL, setup_relay
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType

SAT = "assist_satellite.living_room"
SPEAKER = "media_player.living_room"
TTS = "/api/tts_proxy/abc.mp3"
ALL_SHIPPED = ["intent_sounds", "chime_on_success", "quiet_on_no_match", "chime_on_error"]


def card(title: str) -> dict:
    return {"card": {"tts_relay": {"title": title, "content": ""}}}


def error(code: str) -> dict:
    return {"response_type": "error", "data": {"code": code}}


async def reply(
    hass, relay, response: dict, satellite: str = SAT, acknowledged: bool = False
) -> None:
    for event in (
        PipelineEvent(PipelineEventType.INTENT_END, {"intent_output": {"response": response}}),
        PipelineEvent(PipelineEventType.TTS_START, {"acknowledge_override": acknowledged}),
        PipelineEvent(PipelineEventType.TTS_END, {"tts_output": {"url": TTS}}),
    ):
        relay.handle_event(satellite, event)
        await hass.async_block_till_done(wait_background_tasks=True)


def route(**extra) -> list[dict]:
    return [{"name": "Living room", "satellites": [SAT], "target": SPEAKER, **extra}]


def write(hass, folder: str, name: str, content: str | bytes) -> None:
    path = Path(hass.config.path("tts_relay", folder, name))
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)


async def test_by_default_only_intent_sounds(hass, players):
    relay = await setup_relay(hass, players, route(), sounds={"acknowledge": "/local/ack.mp3"})
    assert [r.id for r in relay.rules_for(relay.route_for(SAT))] == ["intent_sounds"]

    await reply(hass, relay, card("acknowledge"))
    await reply(hass, relay, card("silent"))
    await reply(hass, relay, error("no_intent_match"))
    assert players.urls() == [BASE_URL + "/local/ack.mp3", BASE_URL + TTS]


async def test_all_shipped_rules(hass, players):
    relay = await setup_relay(
        hass,
        players,
        route(rules=ALL_SHIPPED),
        sounds={"acknowledge": "/local/ack.mp3", "error": "/local/error.mp3"},
    )
    await reply(hass, relay, card("acknowledge"))
    await reply(hass, relay, card("silent"))
    await reply(hass, relay, error("no_intent_match"))
    await reply(hass, relay, error("failed_to_handle"))
    await reply(hass, relay, card("timer"))
    await reply(hass, relay, {"speech": {"plain": {"speech": "Done"}}})
    assert players.urls() == [
        BASE_URL + "/local/ack.mp3",
        BASE_URL + "/local/error.mp3",
        BASE_URL + TTS,
        BASE_URL + TTS,
    ]


async def test_chime_on_error_needs_its_sound(hass, players):
    relay = await setup_relay(hass, players, route(rules=["chime_on_error"]))
    await reply(hass, relay, error("failed_to_handle"))
    assert players.urls() == [BASE_URL + TTS]


async def test_card_beats_error_mapping(hass, players):
    relay = await setup_relay(hass, players, route(rules=ALL_SHIPPED), sounds={"error": "/e.mp3"})
    await reply(hass, relay, {**error("failed_to_handle"), **card("silent")})
    assert players.played == []


async def test_own_rules_and_sounds(hass, players, tmp_path):
    relay = await setup_relay(hass, players, route(rules=["intent_sounds", "speak_up"]))
    write(hass, "rules", "intent_sounds.yaml", "name: Mine\nreply: \"{{ 'drop' }}\"\n")
    write(
        hass,
        "rules",
        "speak_up.yaml",
        "name: Speak up\ndescription: Always the speech.\npriority: 1\nreply: speech\n",
    )
    write(hass, "rules", "broken.yaml", "name: Broken\nreply: '{{ oops'\n")
    write(hass, "rules", "incomplete.yaml", "name: No reply\n")
    write(hass, "sounds", "boing.mp3", b"\xff\xfb" + b"\0" * 2000)
    write(hass, "sounds", "notes.txt", "not a sound")
    await hass.services.async_call("tts_relay", "reload", blocking=True)

    assert relay.rules["intent_sounds"].name == "Mine"
    assert "broken" not in relay.rules
    assert "incomplete" not in relay.rules
    assert relay.sounds == {"boing": "/tts_relay/sounds/boing.mp3"}
    assert [r.id for r in relay.rules_for(relay.route_for(SAT))] == ["speak_up", "intent_sounds"]

    await reply(hass, relay, card("silent"))
    assert players.urls() == [BASE_URL + TTS]

    await hass.async_start()
    url = f"http://127.0.0.1:{hass.data['test_port']}/tts_relay/sounds/boing.mp3"
    async with aiohttp.ClientSession() as session, session.get(url) as response:
        assert response.status == 200


async def test_sound_by_name_and_bad_results(hass, players):
    relay = await setup_relay(hass, players, route(rules=["pick"]), sounds={"boing": "/b.mp3"})
    write(
        hass,
        "rules",
        "pick.yaml",
        "name: Pick\nreply: >-\n"
        "  {% if speech == 'a' %}boing{% elif speech == 'b' %}https://x.test/y.mp3"
        "{% elif speech == 'c' %}nonsense{% else %}{{ speech | int }}{% endif %}\n",
    )
    await hass.services.async_call("tts_relay", "reload", blocking=True)

    for speech in ("a", "b", "c", "d"):
        await reply(hass, relay, {"speech": {"plain": {"speech": speech}}})
    assert players.urls() == [BASE_URL + "/b.mp3", "https://x.test/y.mp3"] + [BASE_URL + TTS] * 2


async def test_rule_variables(hass, players):
    relay = await setup_relay(hass, players, route(rules=["echo"]))
    write(
        hass,
        "rules",
        "echo.yaml",
        "name: Echo\nreply: >-\n"
        "  /{{ satellite }}/{{ target }}/{{ speech }}/{{ error_code }}/{{ tts_url }}\n",
    )
    await hass.services.async_call("tts_relay", "reload", blocking=True)
    await reply(hass, relay, {"speech": {"plain": {"speech": "hi"}}})
    assert players.urls() == [f"{BASE_URL}/{SAT}/{SPEAKER}/hi/None/{TTS}"]


def done(*targets: str, response_type: str = "action_done") -> dict:
    success = [{"type": "entity", "name": t, "id": f"light.{t}"} for t in targets]
    return {"response_type": response_type, "data": {"success": success, "failed": []}}


async def test_chime_on_success(hass, players):
    relay = await setup_relay(
        hass, players, route(rules=["chime_on_success"]), sounds={"acknowledge": "/ack.mp3"}
    )
    await reply(hass, relay, done("kitchen"))
    await reply(hass, relay, done(), acknowledged=True)
    await reply(hass, relay, done())
    await reply(hass, relay, done("kitchen", response_type="query_answer"))
    await reply(hass, relay, error("failed_to_handle"))
    assert players.urls() == [BASE_URL + "/ack.mp3"] * 2 + [BASE_URL + TTS] * 3


async def test_chime_on_success_needs_its_sound(hass, players):
    relay = await setup_relay(hass, players, route(rules=["chime_on_success"]))
    await reply(hass, relay, done("kitchen"))
    await reply(hass, relay, done(), acknowledged=True)
    assert players.urls() == [BASE_URL + TTS] * 2


async def test_acknowledged_lasts_one_reply(hass, players):
    relay = await setup_relay(hass, players, route(rules=["echo"]))
    write(hass, "rules", "echo.yaml", "name: Echo\nreply: '/{{ acknowledged }}'\n")
    await hass.services.async_call("tts_relay", "reload", blocking=True)

    await reply(hass, relay, done(), acknowledged=True)
    relay.handle_event(SAT, PipelineEvent(PipelineEventType.RUN_START, {}))
    relay.handle_event(SAT, PipelineEvent(PipelineEventType.TTS_END, {"tts_output": {"url": TTS}}))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert players.urls() == [BASE_URL + "/True", BASE_URL + "/False"]

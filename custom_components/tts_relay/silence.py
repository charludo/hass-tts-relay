from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode, urlparse

from aiohttp import web
from homeassistant.components import tts
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DATA_RELAY, SOUNDS_URL_PATH
from .rules import user_dir

_LOGGER = logging.getLogger(__name__)

REPLY_PATH = "/api/tts_relay/reply"  # /<token>: the real reply, for the speaker
SILENT_PATH = "/api/tts_relay/silent"  # /<name>.<ext>?src=<url>: a silent copy
WAV_PATH = "/api/tts_relay/wav"  # /<token>.wav: the real reply as WAV, for the speaker
SILENT_URL_LIFETIME = timedelta(minutes=5)

CONTENT_TYPES = {"mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav"}
CHUNK = 64 * 1024

_SILENCED = "_tts_relay_silenced"


class SilenceError(Exception):
    """ffmpeg couldn't make the copy."""


def ffmpeg_ok(hass: HomeAssistant) -> bool:
    relay = hass.data.get(DATA_RELAY)
    return relay is None or relay.ffmpeg_ok


async def async_ffmpeg_available(hass: HomeAssistant) -> bool:
    return await hass.async_add_executor_job(shutil.which, _binary(hass)) is not None


def _ffmpeg_lost(hass: HomeAssistant) -> None:
    if (relay := hass.data.get(DATA_RELAY)) is not None:
        relay.set_ffmpeg(False)


def reply_path(token: str) -> str:
    return f"{REPLY_PATH}/{token}"


def wav_path(token: str) -> str:
    # Ends in .wav, which is all Sonos looks at.
    return f"{WAV_PATH}/{token}.wav"


def needs_wav(hass: HomeAssistant, token: str | None) -> bool:
    """Whether the reply is in a format that's worth converting, and can be."""
    try:
        stream = tts.async_get_stream(hass, token) if token else None
    except KeyError:  # No text-to-speech set up.
        return False
    return stream is not None and stream.extension not in ("wav", "mp3") and ffmpeg_ok(hass)


def silence_stream(hass: HomeAssistant, token: str | None) -> bool:
    """Have everyone but our reply view read this reply silently. Return if it's silent.

    Satellites all read their reply from this stream, through tts_proxy or the token.
    """
    try:
        stream = tts.async_get_stream(hass, token) if token else None
    except KeyError:  # No text-to-speech set up.
        stream = None
    if stream is None:
        return False
    if getattr(stream, _SILENCED, False):
        return True
    if not ffmpeg_ok(hass):
        return False
    if stream.extension not in CONTENT_TYPES:
        _LOGGER.warning("Can't make %s silent, the satellite will be heard", stream.extension)
        return False
    real = tts.ResultStream.async_stream_result

    async def silent() -> AsyncGenerator[bytes]:
        started = False
        try:
            async for chunk in silent_copy(hass, real(stream), stream.extension):
                started = True
                yield chunk
        except SilenceError as err:
            if started:
                raise
            # Rather heard than missing.
            _LOGGER.warning("Can't make the reply silent, the satellite will be heard: %s", err)
            async for chunk in real(stream):
                yield chunk

    stream.async_stream_result = silent  # type: ignore[method-assign]
    setattr(stream, _SILENCED, True)
    return True


def silent_url(hass: HomeAssistant, source: str) -> str:
    """A signed URL to a silent copy of a file, as long as the file."""
    extension = PurePosixPath(urlparse(source).path).suffix.lower().removeprefix(".")
    if extension not in CONTENT_TYPES:
        extension = "wav"
    path = f"{SILENT_PATH}/announcement.{extension}?{urlencode({'src': source})}"
    return async_sign_path(hass, path, SILENT_URL_LIFETIME, use_content_user=True)


def base_url(hass: HomeAssistant) -> str | None:
    try:
        return get_url(hass, allow_internal=True, prefer_external=False).rstrip("/")
    except NoURLAvailableError:
        return None


def local_file(hass: HomeAssistant, url: str) -> Path | None:
    """The file on disk behind a sound's URL, if it's one Home Assistant serves."""
    parsed = urlparse(url)
    base = base_url(hass)
    if parsed.netloc and (base is None or parsed.netloc != urlparse(base).netloc):
        return None
    relative = PurePosixPath(parsed.path)
    for prefix, folder in (
        (PurePosixPath(SOUNDS_URL_PATH), user_dir(hass, "sounds")),
        (PurePosixPath("/local"), Path(hass.config.path("www"))),
    ):
        if relative.is_relative_to(prefix):
            path = (folder / relative.relative_to(prefix)).resolve()
            if path.is_relative_to(folder.resolve()) and path.is_file():
                return path
    return None


async def silent_copy(
    hass: HomeAssistant, audio: AsyncIterator[bytes], extension: str
) -> AsyncGenerator[bytes]:
    """The audio silenced as it comes in, in the same format and as long."""
    if extension == "wav":
        async for chunk in _silent_wav(audio):
            yield chunk
        return
    async for chunk in _ffmpeg(hass, ["-f", extension, "-i", "pipe:0"], audio, extension):
        yield chunk


async def wav_copy(
    hass: HomeAssistant, audio: AsyncIterator[bytes], extension: str
) -> AsyncGenerator[bytes]:
    """The audio as WAV, as it comes in."""
    async for chunk in _ffmpeg(hass, ["-f", extension, "-i", "pipe:0"], audio, "wav", silent=False):
        yield chunk


async def _silent_wav(audio: AsyncIterator[bytes]) -> AsyncGenerator[bytes]:
    # Keep the header, zero the samples.
    header: bytes | None = b""
    async for chunk in audio:
        if header is None:
            yield bytes(len(chunk))
            continue
        header += chunk
        if (start := _wav_data_start(header)) is None:
            continue
        yield header[:start]
        yield bytes(len(header) - start)
        header = None


def _wav_data_start(header: bytes) -> int | None:
    """Where the samples begin, or None if the header isn't complete yet."""
    if len(header) < 12:
        return None
    if header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return 0  # Raw samples.
    position = 12
    while position + 8 <= len(header):
        size = int.from_bytes(header[position + 4 : position + 8], "little")
        if header[position : position + 4] == b"data":
            return position + 8
        position += 8 + size + (size & 1)
    return None


def _binary(hass: HomeAssistant) -> str:
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except ValueError:  # ffmpeg isn't set up.
        return "ffmpeg"


async def _start_ffmpeg(
    hass: HomeAssistant, source: list[str], fed: bool, extension: str, silent: bool
) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        _binary(hass),
        "-hide_banner",
        "-loglevel",
        "error",
        # Start with the first audio: a reply may still be generated.
        "-probesize",
        "1024",
        "-analyzeduration",
        "0",
        *source,
        *(["-af", "volume=0"] if silent else []),
        "-f",
        extension,
        "-flush_packets",
        "1",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE if fed else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _ffmpeg(
    hass: HomeAssistant,
    source: list[str],
    feed: AsyncIterator[bytes] | None,
    extension: str,
    *,
    silent: bool = True,
) -> AsyncGenerator[bytes]:
    try:
        process = await _start_ffmpeg(hass, source, feed is not None, extension, silent)
    except OSError as err:  # Not installed, or not runnable.
        _ffmpeg_lost(hass)
        raise SilenceError(f"Can't run ffmpeg: {err}") from err
    assert process.stdout is not None and process.stderr is not None

    async def write() -> None:
        assert process.stdin is not None and feed is not None
        try:
            async for chunk in feed:
                process.stdin.write(chunk)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # ffmpeg stopped, which its exit code reports.
        finally:
            process.stdin.close()

    writer = asyncio.create_task(write()) if feed is not None else None
    try:
        while chunk := await process.stdout.read(CHUNK):
            yield chunk
        if await process.wait() != 0:
            error = (await process.stderr.read()).decode(errors="replace").strip()
            raise SilenceError(error or f"ffmpeg exited with {process.returncode}")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if writer is not None:
            writer.cancel()


class ReplyView(HomeAssistantView):
    url = REPLY_PATH + "/{token}"
    name = "api:tts_relay:reply"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _stream(self, token: str) -> tts.ResultStream:
        if (stream := tts.async_get_stream(self.hass, token)) is None:
            raise web.HTTPNotFound
        return stream

    async def head(self, request: web.Request, token: str) -> web.StreamResponse:
        return web.Response(content_type=self._stream(token).content_type)

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        stream = self._stream(token)
        response = web.StreamResponse()
        response.content_type = stream.content_type
        await response.prepare(request)
        async for chunk in tts.ResultStream.async_stream_result(stream):
            await response.write(chunk)
        await response.write_eof()
        return response


class WavView(ReplyView):
    url = WAV_PATH + "/{name}"
    name = "api:tts_relay:wav"

    async def head(self, request: web.Request, name: str) -> web.StreamResponse:
        self._stream(name.removesuffix(".wav"))
        return web.Response(content_type=CONTENT_TYPES["wav"])

    async def get(self, request: web.Request, name: str) -> web.StreamResponse:
        stream = self._stream(name.removesuffix(".wav"))
        real = tts.ResultStream.async_stream_result
        copy = wav_copy(self.hass, real(stream), stream.extension)
        try:
            first = await anext(copy, b"")
        except SilenceError as err:
            # Rather the original than nothing; some speakers may cope.
            _LOGGER.warning("Can't convert the reply to WAV: %s", err)
            return await super().get(request, name.removesuffix(".wav"))
        response = web.StreamResponse()
        response.content_type = CONTENT_TYPES["wav"]
        await response.prepare(request)
        await response.write(first)
        async for chunk in copy:
            await response.write(chunk)
        await response.write_eof()
        return response


class SilentView(HomeAssistantView):
    url = SILENT_PATH + "/{name}"
    name = "api:tts_relay:silent"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get(self, request: web.Request, name: str) -> web.StreamResponse:
        if (source := request.query.get("src")) is None:
            raise web.HTTPBadRequest
        extension = PurePosixPath(name).suffix.removeprefix(".")
        if extension not in CONTENT_TYPES:
            raise web.HTTPNotFound
        path = await self.hass.async_add_executor_job(local_file, self.hass, source)
        copy = _ffmpeg(
            self.hass, ["-i", str(path) if path else self._absolute(source)], None, extension
        )
        try:
            first = await anext(copy, b"")
        except SilenceError as err:
            # Rather heard than missing.
            _LOGGER.warning("Can't make %s silent, the satellite will be heard: %s", source, err)
            return await self._original(request, source, path)
        response = web.StreamResponse()
        response.content_type = CONTENT_TYPES[extension]
        await response.prepare(request)
        await response.write(first)
        async for chunk in copy:
            await response.write(chunk)
        await response.write_eof()
        return response

    def _absolute(self, source: str) -> str:
        if source.startswith(("http://", "https://")):
            return source
        return (base_url(self.hass) or "") + source

    async def _original(
        self, request: web.Request, source: str, path: Path | None
    ) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        if path is not None:
            await response.write(await self.hass.async_add_executor_job(path.read_bytes))
        else:
            async with async_get_clientsession(self.hass).get(self._absolute(source)) as original:
                async for chunk in original.content.iter_chunked(CHUNK):
                    await response.write(chunk)
        await response.write_eof()
        return response

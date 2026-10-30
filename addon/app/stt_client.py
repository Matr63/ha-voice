"""Minimal Wyoming ASR client used by the gate, the diary and the test page."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient

_LOGGER = logging.getLogger("ha-voice.stt-client")


async def _transcribe(
    uri: str, pcm: bytes, rate: int, width: int, channels: int, language: str | None
) -> str:
    async with AsyncClient.from_uri(uri) as client:
        await client.write_event(Transcribe(language=language).event())
        await client.write_event(AudioStart(rate=rate, width=width, channels=channels).event())
        step = max(rate * width * channels // 10, 1024)
        for i in range(0, len(pcm), step):
            await client.write_event(
                AudioChunk(rate=rate, width=width, channels=channels, audio=pcm[i : i + step]).event()
            )
        await client.write_event(AudioStop().event())
        while True:
            event = await client.read_event()
            if event is None:
                return ""
            if Transcript.is_type(event.type):
                return Transcript.from_event(event).text or ""


async def transcribe(
    uri: str,
    pcm: bytes,
    rate: int = 16000,
    width: int = 2,
    channels: int = 1,
    language: str | None = None,
    timeout: float = 120.0,
) -> str:
    if not pcm:
        return ""
    try:
        return (await asyncio.wait_for(_transcribe(uri, pcm, rate, width, channels, language), timeout)).strip()
    except Exception:
        _LOGGER.exception("STT request to %s failed", uri)
        return ""


async def port_open(uri: str, timeout: float = 1.0) -> bool:
    u = urlparse(uri)
    if not u.hostname or not u.port:
        return False
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port), timeout)
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True

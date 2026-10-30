"""Wyoming TTS client for the UI test button."""
from __future__ import annotations

import logging

from wyoming.audio import AudioChunk, AudioStop
from wyoming.client import AsyncClient
from wyoming.tts import Synthesize, SynthesizeVoice

from audio_utils import to_wav
from tts_engine import SAMPLE_RATE

_LOGGER = logging.getLogger("ha-voice.tts-client")


async def synthesize_wav(uri: str, text: str, voice: str | None = None, timeout: float = 60.0) -> bytes:
    import asyncio

    return await asyncio.wait_for(_synthesize_wav(uri, text, voice), timeout)


async def _synthesize_wav(uri: str, text: str, voice: str | None) -> bytes:
    pcm = bytearray()
    rate = SAMPLE_RATE
    async with AsyncClient.from_uri(uri) as client:
        syn = Synthesize(text=text, voice=SynthesizeVoice(name=voice) if voice else None)
        await client.write_event(syn.event())
        while True:
            event = await client.read_event()
            if event is None:
                break
            if event.type == "error":
                raise RuntimeError((event.data or {}).get("text") or "ошибка TTS")
            if AudioChunk.is_type(event.type):
                chunk = AudioChunk.from_event(event)
                rate = chunk.rate or rate
                pcm.extend(chunk.audio)
            elif AudioStop.is_type(event.type):
                break
    if not pcm:
        raise RuntimeError("TTS не вернул звук")
    return to_wav(bytes(pcm), rate)

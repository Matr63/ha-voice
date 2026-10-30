#!/usr/bin/env python3
"""Wyoming TTS server: Kokoro-ru."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from functools import partial
from pathlib import Path
from typing import Any

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.tts import Synthesize

from tts_engine import DEFAULT_VOICE, SAMPLE_RATE, VOICES, KokoroEngine, resolve_voice
from version import __version__

_LOGGER = logging.getLogger("ha-voice.tts")
_CHUNK = SAMPLE_RATE  # 1 s of 16-bit mono


def _info(default_voice: str) -> Info:
    voices = [
        TtsVoice(
            name=key,
            description=f"Kokoro-ru · {meta['label']} ({'жен.' if meta['gender'] == 'female' else 'муж.'})",
            attribution=Attribution(name="zaakirio/kokoro-ru", url="https://huggingface.co/zaakirio/kokoro-ru"),
            installed=True,
            languages=["ru"],
            version="2",
        )
        for key, meta in VOICES.items()
    ]
    # default first so HA picks it when voice is omitted
    voices.sort(key=lambda v: 0 if v.name == default_voice else 1)
    return Info(
        tts=[
            TtsProgram(
                name="ha-voice-kokoro-ru",
                description="Kokoro-ru TTS for Home Assistant (офлайн)",
                attribution=Attribution(name="HA Voice", url="https://github.com/Matr63/ha-voice"),
                installed=True,
                version=__version__,
                voices=voices,
                supports_synthesize_streaming=False,
            )
        ]
    )


class TtsHandler(AsyncEventHandler):
    def __init__(
        self,
        wyoming_info: Info,
        engine: KokoroEngine,
        lock: asyncio.Lock,
        default_voice: str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.wyoming_info_event = wyoming_info.event()
        self.engine = engine
        self.lock = lock
        self.default_voice = default_voice
        self._settings_path = Path(os.environ.get("SETTINGS_FILE", "/data/settings.json"))

    def _default_voice(self) -> str:
        try:
            import json

            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
            return resolve_voice(str(data.get("tts_voice") or self.default_voice))
        except Exception:
            return self.default_voice

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info_event)
            return True
        if not Synthesize.is_type(event.type):
            return True
        req = Synthesize.from_event(event)
        if req.voice and req.voice.name:
            voice = resolve_voice(req.voice.name)
        else:
            voice = self._default_voice()
        try:
            async with self.lock:
                pcm = await asyncio.to_thread(self.engine.synthesize, req.text, voice)
        except Exception as err:
            _LOGGER.exception("TTS failed")
            await self.write_event(Error(text=str(err), code=err.__class__.__name__).event())
            return True

        await self.write_event(AudioStart(rate=SAMPLE_RATE, width=2, channels=1).event())
        step = _CHUNK * 2
        for i in range(0, len(pcm), step):
            await self.write_event(
                AudioChunk(rate=SAMPLE_RATE, width=2, channels=1, audio=pcm[i : i + step]).event()
            )
        await self.write_event(AudioStop().event())
        _LOGGER.info("tts voice=%s chars=%s samples=%s", voice, len(req.text or ""), len(pcm) // 2)
        return True


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("TTS_URI", "tcp://0.0.0.0:10351"))
    parser.add_argument("--voice", default=os.environ.get("TTS_VOICE", DEFAULT_VOICE))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug or os.environ.get("DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    default_voice = resolve_voice(args.voice)
    engine = KokoroEngine()
    engine.load()

    info = _info(default_voice)
    server = AsyncServer.from_uri(args.uri)
    lock = asyncio.Lock()
    _LOGGER.info("TTS listening on %s default_voice=%s", args.uri, default_voice)
    await server.run(partial(TtsHandler, info, engine, lock, default_voice))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

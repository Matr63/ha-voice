"""Wyoming STT proxy for Assist: only enrolled voices get a transcript (blocks TV / strangers)."""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timezone
from functools import partial
from typing import Any

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

from audio_utils import normalize_pcm
from settings import SettingsStore
from speakers import SpeakerStore
from stt_client import transcribe
from version import __version__

_LOGGER = logging.getLogger("ha-voice.gate")
_REPO = "https://github.com/Matr63/ha-voice"
_background: set[asyncio.Task] = set()


class GateState:
    def __init__(self, maxlen: int = 300) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self.accepted = 0
        self.rejected = 0
        self._next_id = 1

    def add(self, event: dict[str, Any]) -> dict[str, Any]:
        event["id"] = self._next_id
        self._next_id += 1
        if event["accepted"]:
            self.accepted += 1
        else:
            self.rejected += 1
        self.events.appendleft(event)
        return event

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self.events)[: max(1, limit)]


class GateHandler(AsyncEventHandler):
    def __init__(
        self,
        info_event: Event,
        store: SpeakerStore,
        settings: SettingsStore,
        upstream: str,
        state: GateState,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.info_event = info_event
        self.store = store
        self.settings = settings
        self.upstream = upstream
        self.state = state
        self.language: str | None = None
        self.audio = bytearray()
        self.rate = 16000
        self.width = 2
        self.channels = 1

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.info_event)
        elif Transcribe.is_type(event.type):
            self.language = Transcribe.from_event(event).language
        elif AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self.rate, self.width, self.channels = start.rate, start.width, start.channels
            self.audio = bytearray()
        elif AudioChunk.is_type(event.type):
            self.audio.extend(AudioChunk.from_event(event).audio)
        elif AudioStop.is_type(event.type):
            await self._finish()
        return True

    async def _finish(self) -> None:
        raw = bytes(self.audio)
        self.audio = bytearray()
        bytes_per_sec = self.rate * self.width * self.channels
        event: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "duration_ms": int(len(raw) / bytes_per_sec * 1000) if bytes_per_sec else 0,
            "speaker_id": None,
            "speaker_name": None,
            "score": None,
            "matched": False,
            "accepted": True,
            "reason": "no_speakers",
            "text": "",
        }

        if raw and self.width == 2 and self.store.has_speakers():
            pcm16 = normalize_pcm(raw, self.rate, self.channels)
            match = await asyncio.to_thread(self.store.match, pcm16)
            event.update(
                speaker_id=match.speaker_id,
                speaker_name=match.name,
                score=match.score,
                matched=match.matched,
            )
            if match.matched:
                event["reason"] = "match"
            elif self.settings.get().gate_require_match:
                event.update(accepted=False, reason="unknown_voice")
            else:
                event["reason"] = "filter_off"

        record = self.state.add(event)
        _LOGGER.info(
            "assist %s reason=%s speaker=%s score=%s",
            "accepted" if record["accepted"] else "REJECTED",
            record["reason"],
            record["speaker_name"],
            record["score"],
        )

        if not record["accepted"]:
            await self.write_event(Transcript(text="").event())
            task = asyncio.create_task(self._fill_text(raw, record))
            _background.add(task)
            task.add_done_callback(_background.discard)
            return

        record["text"] = await self._transcribe(raw)
        await self.write_event(Transcript(text=record["text"]).event())

    async def _transcribe(self, raw: bytes) -> str:
        return await transcribe(self.upstream, raw, self.rate, self.width, self.channels, self.language)

    async def _fill_text(self, raw: bytes, record: dict[str, Any]) -> None:
        record["text"] = await self._transcribe(raw)


def _info() -> Info:
    attribution = Attribution(name="ha-voice", url=_REPO)
    return Info(
        asr=[
            AsrProgram(
                name="ha-voice-speaker-gate",
                description="GigaAM, принимает команды только от записанных голосов",
                attribution=attribution,
                installed=True,
                version=__version__,
                models=[
                    AsrModel(
                        name="gated-gigaam",
                        description="GigaAM behind speaker verification",
                        attribution=attribution,
                        installed=True,
                        languages=["ru", "en"],
                        version=__version__,
                    )
                ],
            )
        ]
    )


async def run_gate(
    uri: str,
    upstream: str,
    store: SpeakerStore,
    settings: SettingsStore,
    state: GateState,
) -> None:
    server = AsyncServer.from_uri(uri)
    _LOGGER.info("Speaker gate on %s → %s", uri, upstream)
    await server.run(partial(GateHandler, _info().event(), store, settings, upstream, state))

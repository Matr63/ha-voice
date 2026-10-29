"""Wyoming STT proxy: verify enrolled speaker, else reject (blocks TV / strangers)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from functools import partial
from typing import Any
from urllib.parse import urlparse

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

from speakers import SpeakerStore

_LOGGER = logging.getLogger("ha-voice.gate")


def _parse_tcp(uri: str) -> tuple[str, int]:
    u = urlparse(uri)
    if u.scheme != "tcp" or not u.hostname or not u.port:
        raise ValueError(f"Expected tcp://host:port, got {uri}")
    return u.hostname, int(u.port)


class GateHandler(AsyncEventHandler):
    def __init__(
        self,
        wyoming_info: Info,
        store: SpeakerStore,
        upstream_uri: str,
        require_match: bool,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.wyoming_info_event = wyoming_info.event()
        self.store = store
        self.upstream_uri = upstream_uri
        self.require_match = require_match
        self.audio_buffer = bytearray()
        self.sample_rate = 16000
        self.sample_width = 2
        self.channels = 1
        self.language: str | None = None
        self.last_match: dict[str, Any] | None = None

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info_event)
            return True

        if Transcribe.is_type(event.type):
            tr = Transcribe.from_event(event)
            self.language = tr.language
            self.audio_buffer = bytearray()
            return True

        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self.sample_rate = start.rate
            self.sample_width = start.width
            self.channels = start.channels
            self.audio_buffer = bytearray()
            return True

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            self.audio_buffer.extend(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            pcm = bytes(self.audio_buffer)
            self.audio_buffer = bytearray()
            match = self.store.match_pcm(pcm, self.sample_rate)
            self.last_match = {
                "speaker_id": match.speaker_id,
                "name": match.name,
                "score": match.score,
                "matched": match.matched,
            }
            _LOGGER.info(
                "speaker match name=%s score=%.3f matched=%s",
                match.name,
                match.score,
                match.matched,
            )

            if self.require_match and not match.matched:
                await self.write_event(Transcript(text="").event())
                return True

            text = await self._upstream_transcribe(pcm)
            if match.matched and match.name and text:
                # prefix for HA automations / logs (Assist still gets plain intent text)
                pass
            await self.write_event(Transcript(text=text).event())
            return True

        return True

    async def _upstream_transcribe(self, pcm: bytes) -> str:
        if not pcm:
            return ""
        try:
            async with AsyncClient.from_uri(self.upstream_uri) as client:
                if self.language:
                    await client.write_event(Transcribe(language=self.language).event())
                await client.write_event(
                    AudioStart(
                        rate=self.sample_rate,
                        width=self.sample_width,
                        channels=self.channels,
                    ).event()
                )
                # chunk to avoid huge frames
                step = self.sample_rate * self.sample_width * self.channels // 5
                for i in range(0, len(pcm), max(step, 3200)):
                    piece = pcm[i : i + max(step, 3200)]
                    await client.write_event(
                        AudioChunk(
                            rate=self.sample_rate,
                            width=self.sample_width,
                            channels=self.channels,
                            audio=piece,
                        ).event()
                    )
                await client.write_event(AudioStop().event())
                while True:
                    ev = await client.read_event()
                    if ev is None:
                        break
                    if Transcript.is_type(ev.type):
                        return Transcript.from_event(ev).text or ""
        except Exception:
            _LOGGER.exception("Upstream STT failed")
            return ""
        return ""


async def run_gate(
    uri: str,
    upstream: str,
    store: SpeakerStore,
    require_match: bool,
) -> None:
    info = Info(
        asr=[
            AsrProgram(
                name="ha-voice-speaker-gate",
                description="Speaker-gated STT (rejects unknown / TV)",
                attribution=Attribution(name="ha-voice", url="https://localhost"),
                installed=True,
                version="1.0.0",
                models=[
                    AsrModel(
                        name="gated-gigaam",
                        description="GigaAM behind speaker verification",
                        attribution=Attribution(name="ha-voice", url="https://localhost"),
                        installed=True,
                        languages=["ru", "en"],
                        version="1.0.0",
                    )
                ],
            )
        ]
    )
    server = AsyncServer.from_uri(uri)
    _LOGGER.info("Speaker gate listening on %s → %s require_match=%s", uri, upstream, require_match)
    await server.run(partial(GateHandler, info, store, upstream, require_match))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default=os.environ.get("GATE_URI", "tcp://0.0.0.0:10350"))
    parser.add_argument("--upstream", default=os.environ.get("GATE_UPSTREAM", "tcp://127.0.0.1:10305"))
    parser.add_argument("--speakers-dir", default=os.environ.get("SPEAKERS_DIR", "/data/speakers"))
    parser.add_argument("--threshold", type=float, default=float(os.environ.get("GATE_THRESHOLD", "0.55")))
    parser.add_argument(
        "--require-match",
        default=os.environ.get("GATE_REQUIRE_MATCH", "true"),
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    require = str(args.require_match).lower() in ("1", "true", "yes", "on")
    store = SpeakerStore(args.speakers_dir, threshold=args.threshold)
    asyncio.run(run_gate(args.uri, args.upstream, store, require))


if __name__ == "__main__":
    main()

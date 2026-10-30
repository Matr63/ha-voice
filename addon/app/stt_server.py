#!/usr/bin/env python3
"""Wyoming STT server powered by onnx-asr (GigaAM and others)."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from functools import partial
from typing import Any

import numpy as np
import onnx_asr
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

_LOGGER = logging.getLogger("ha-voice.stt")


class OnnxAsrHandler(AsyncEventHandler):
    def __init__(
        self,
        wyoming_info: Info,
        model: Any,
        model_lock: asyncio.Lock,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.wyoming_info_event = wyoming_info.event()
        self.model = model
        self.model_lock = model_lock
        self.audio_buffer: bytearray | None = None
        self.language: str | None = None
        self.sample_rate = 16000
        self.channels = 1

    def _recognize(self, audio: np.ndarray) -> str:
        kwargs: dict[str, Any] = {"sample_rate": self.sample_rate}
        if self.language:
            try:
                return str(self.model.recognize(audio, language=self.language, **kwargs) or "")
            except TypeError:
                pass
        return str(self.model.recognize(audio, **kwargs) or "")

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info_event)
            return True

        if Transcribe.is_type(event.type):
            tr = Transcribe.from_event(event)
            self.language = tr.language
            self.audio_buffer = None
            return True

        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self.sample_rate = start.rate
            self.channels = start.channels
            self.audio_buffer = bytearray()
            return True

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            if self.audio_buffer is None:
                self.audio_buffer = bytearray()
            self.audio_buffer.extend(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            buf = self.audio_buffer or bytearray()
            self.audio_buffer = None
            if not buf:
                await self.write_event(Transcript(text="").event())
                return True

            audio_f32 = np.frombuffer(bytes(buf[: len(buf) - len(buf) % 2]), dtype=np.int16).astype(np.float32) / 32768.0
            if self.channels > 1:
                usable = audio_f32.size - audio_f32.size % self.channels
                audio_f32 = audio_f32[:usable].reshape(-1, self.channels).mean(axis=1)

            async with self.model_lock:
                text = await asyncio.to_thread(self._recognize, audio_f32)

            _LOGGER.info("transcript=%r", text)
            await self.write_event(Transcript(text=str(text or "")).event())
            self.language = None
            return True

        return True


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=os.environ.get("STT_MODEL", "gigaam-v3-e2e-rnnt"))
    parser.add_argument("--uri", default=os.environ.get("STT_URI", "tcp://0.0.0.0:10305"))
    parser.add_argument("--device", default=os.environ.get("STT_DEVICE", "cpu"))
    parser.add_argument("--quantization", default=os.environ.get("STT_QUANTIZATION") or None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    providers = ["CPUExecutionProvider"]
    if args.device == "cuda":
        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"]

    _LOGGER.info(
        "Loading model=%s quant=%s device=%s",
        args.model,
        args.quantization or "fp32",
        args.device,
    )
    load_kwargs: dict[str, Any] = {"providers": providers}
    if args.quantization:
        load_kwargs["quantization"] = args.quantization
    model = onnx_asr.load_model(args.model, **load_kwargs)
    _LOGGER.info("Model ready")

    info = Info(
        asr=[
            AsrProgram(
                name="ha-voice-onnx-asr",
                description="GigaAM / ONNX ASR for Home Assistant",
                attribution=Attribution(name="onnx-asr", url="https://github.com/istupakov/onnx-asr"),
                installed=True,
                version="1.0.0",
                models=[
                    AsrModel(
                        name=args.model,
                        description=f"ONNX model: {args.model}",
                        attribution=Attribution(
                            name="HuggingFace",
                            url=f"https://huggingface.co/istupakov/{args.model}",
                        ),
                        installed=True,
                        languages=["ru", "en"],
                        version=args.quantization or "fp32",
                    )
                ],
            )
        ]
    )

    server = AsyncServer.from_uri(args.uri)
    lock = asyncio.Lock()
    _LOGGER.info("Listening on %s", args.uri)
    await server.run(partial(OnnxAsrHandler, info, model, lock))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

"""Wake word on the PC: openWakeWord in-process (ONNX) or a Wyoming wake service."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from settings import SettingsStore

_LOGGER = logging.getLogger("ha-voice.wake")

FRAME_SAMPLES = 1280  # 80 ms at 16 kHz, what openWakeWord expects
REFRACTORY_S = 2.0
PEAK_FRAMES = 200  # 16 s of scores: an utterance plus the recognition queue

# Built-in openWakeWord models and how GigaAM tends to spell them, so the wake
# word can be cut from the start of the command text.
PRETRAINED: dict[str, dict[str, Any]] = {
    "alexa": {"label": "Алекса", "spoken": ["алекса", "алекс", "алекся", "alexa"]},
    "hey_jarvis": {"label": "Эй, Джарвис", "spoken": ["эй джарвис", "хей джарвис", "джарвис"]},
    "hey_mycroft": {"label": "Эй, Майкрофт", "spoken": ["эй майкрофт", "хей майкрофт", "майкрофт"]},
    "hey_rhasspy": {"label": "Эй, Распи", "spoken": ["эй распи", "хей распи", "распи"]},
}


def custom_dir() -> Path:
    return Path(os.environ.get("WAKEWORDS_DIR", "/data/wakewords"))


NO_MODEL = "none"


def available_models() -> list[dict[str, str]]:
    out = [{"id": k, "label": v["label"], "kind": "builtin"} for k, v in PRETRAINED.items()]
    out.append({"id": NO_MODEL, "label": "Выключено — только свои фразы", "kind": "off"})
    d = custom_dir()
    if d.is_dir():
        for f in sorted(d.glob("*.onnx")):
            out.append({"id": f.stem, "label": f.stem.replace("_", " "), "kind": "custom"})
    return out


def strip_wake_word(text: str, wake_word: str) -> str:
    """'Алекса, включи свет' -> 'включи свет'."""
    spoken = PRETRAINED.get(wake_word, {}).get("spoken") or [wake_word.replace("_", " ")]
    t = text.strip()
    low = t.lower()
    for phrase in sorted(spoken, key=len, reverse=True):
        if low.startswith(phrase):
            return re.sub(r"^[\s,.!?:;—-]+", "", t[len(phrase):])
    return t


def model_phrases(wake_word: str) -> list[str]:
    """How STT writes the built-in model's word: a backup when the model misses
    it (Russian «Алекса» scores close to the threshold of the English model)."""
    return [normalize(p) for p in PRETRAINED.get(wake_word, {}).get("spoken", [])]


def normalize(text: str) -> str:
    """'Алекса, НЕ спи!' -> 'алекса не спи'."""
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s]", " ", t)
    return " ".join(t.split())


def phrase_list(raw: str) -> list[str]:
    return sorted({normalize(p) for p in raw.split(",") if normalize(p)}, key=len, reverse=True)


def match_prefix(text: str, phrases: list[str]) -> tuple[str, str] | None:
    """('алекса', 'включи свет') when the text starts with one of the phrases."""
    norm = normalize(text)
    for phrase in phrases:
        if norm == phrase or norm.startswith(phrase + " "):
            return phrase, norm[len(phrase):].strip()
    return None


def match_anywhere(text: str, phrases: list[str]) -> tuple[str, str] | None:
    """('алекса', 'включи свет') when a whole-word phrase sits anywhere in the text.

    The check window often starts with the TV, so the wake word is not a prefix.
    """
    words = normalize(text).split()
    if not words:
        return None
    for phrase in phrases:
        parts = phrase.split()
        n = len(parts)
        if not n or n > len(words):
            continue
        for i in range(len(words) - n + 1):
            if words[i : i + n] == parts:
                return phrase, " ".join(words[i + n :])
    return None


def confirm_phrases(wake_word: str, wake_phrases: str) -> list[str]:
    """What the second pass must hear: the model's spellings plus custom phrases."""
    spoken = model_phrases(wake_word)
    if not spoken and wake_word and wake_word != NO_MODEL:
        spoken = [normalize(wake_word.replace("_", " "))]
    return phrase_list(",".join([*spoken, wake_phrases]))


# The second pass is one STT call on a short clip. Longer than this is a miss:
# waiting out the TV would bring the false hits back.
CONFIRM_TIMEOUT_S = 3.0


class WakeVerdict:
    """Result of the second pass. A later model fills the same fields."""

    def __init__(self, accepted: bool, heard: str, phrase: str) -> None:
        self.accepted = accepted
        self.heard = heard
        self.phrase = phrase


class WakeConfirm:
    """Audio around a streaming wake hit. The satellite pushes frames and reads a verdict.

    The window is a fixed time after the hit, not a pause. Today the verdict is one
    call to the existing recogniser; a streaming model replaces run() only.
    """

    def __init__(self, pre: bytes, phrases: list[str], tail_ms: int, stt_uri: str) -> None:
        self.pre = pre
        self.phrases = list(phrases)
        self.tail_ms = max(0, int(tail_ms))
        self.stt_uri = stt_uri
        self._tail = bytearray()
        self._after = bytearray()
        self._window = b""
        self._cmd_tail = b""
        self._opened = time.monotonic()
        self.pending = False
        self.done = False

    def push(self, pcm: bytes) -> None:
        if self.pending or self.done:
            self._after.extend(pcm)
        else:
            self._tail.extend(pcm)

    def due(self) -> bool:
        if self.pending or self.done:
            return False
        return (time.monotonic() - self._opened) * 1000.0 >= self.tail_ms

    def begin(self) -> None:
        if self.pending or self.done:
            return
        self._window = self.pre + bytes(self._tail)
        self._cmd_tail = bytes(self._tail)
        self.pending = True

    def command_audio(self) -> bytes:
        """Frames from the hit until the verdict: the command may already have started."""
        return self._cmd_tail + bytes(self._after)

    def finish(self, heard: str) -> WakeVerdict:
        text = (heard or "").strip()
        hit = match_anywhere(text, self.phrases) if text else None
        self.done = True
        self.pending = False
        return WakeVerdict(accepted=hit is not None, heard=text, phrase=hit[0] if hit else "")

    async def run(self) -> WakeVerdict:
        from stt_client import transcribe

        self.begin()
        heard = await transcribe(self.stt_uri, self._window, timeout=CONFIRM_TIMEOUT_S)
        return self.finish(heard)


# Two boards load their models in parallel threads; importing openwakeword from
# both at once fails with "partially initialized module".
_LOAD_LOCK = threading.Lock()
RETRY_FAILED_S = 30.0


def _resolve_model(name: str) -> str:
    custom = custom_dir() / f"{name}.onnx"
    if custom.is_file():
        return str(custom)
    return name


class LocalWake:
    """openWakeWord running in this process. One instance per satellite stream."""

    def __init__(self, model: str) -> None:
        with _LOAD_LOCK:
            from openwakeword.model import Model  # heavy import, only when used

            self.name = model
            self._model = Model(wakeword_models=[_resolve_model(model)], inference_framework="onnx")
        self._buf = np.zeros(0, dtype=np.int16)
        self._last_hit = 0.0
        self.last_score = 0.0
        self._recent: deque[tuple[float, float]] = deque(maxlen=PEAK_FRAMES)

    def peak(self, window_s: float) -> float:
        since = time.monotonic() - window_s
        return max((sc for t, sc in self._recent if t >= since), default=0.0)

    def process(self, pcm: bytes, threshold: float) -> float | None:
        """Returns the score when the wake word fired, else None."""
        samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
        self._buf = np.concatenate([self._buf, samples])
        hit: float | None = None
        while self._buf.size >= FRAME_SAMPLES:
            frame = self._buf[:FRAME_SAMPLES]
            self._buf = self._buf[FRAME_SAMPLES:]
            scores = self._model.predict(frame)
            score = float(max(scores.values())) if scores else 0.0
            self.last_score = score
            now = time.monotonic()
            self._recent.append((now, score))
            if score >= threshold and now - self._last_hit > REFRACTORY_S:
                self._last_hit = now
                hit = score
        return hit

    def reset(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)
        try:
            self._model.reset()
        except Exception:
            pass


class WyomingWake:
    """Streams audio to a Wyoming wake service (e.g. the openWakeWord add-on)."""

    def __init__(self, uri: str, model: str) -> None:
        self.uri = uri
        self.name = model
        self._client: Any = None
        self._reader: asyncio.Task | None = None
        self._hits: asyncio.Queue[float] = asyncio.Queue()
        self._last_try = 0.0
        self.last_score = 0.0

    async def _connect(self) -> bool:
        if self._client is not None:
            return True
        if time.monotonic() - self._last_try < 5:
            return False
        self._last_try = time.monotonic()
        from wyoming.audio import AudioStart
        from wyoming.client import AsyncClient
        from wyoming.wake import Detect

        try:
            client = AsyncClient.from_uri(self.uri)
            await client.connect()
            await client.write_event(Detect(names=[self.name]).event())
            await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        except Exception as err:
            _LOGGER.warning("Wake service %s unavailable: %s", self.uri, err)
            return False
        self._client = client
        self._reader = asyncio.create_task(self._read())
        _LOGGER.info("Wake service connected %s (%s)", self.uri, self.name)
        return True

    async def _read(self) -> None:
        from wyoming.wake import Detection

        try:
            while self._client is not None:
                event = await self._client.read_event()
                if event is None:
                    break
                if Detection.is_type(event.type):
                    self._hits.put_nowait(1.0)
        except Exception:
            _LOGGER.exception("Wake service read failed")
        await self.close()

    async def feed(self, pcm: bytes) -> float | None:
        if not await self._connect():
            return None
        from wyoming.audio import AudioChunk

        try:
            await self._client.write_event(AudioChunk(rate=16000, width=2, channels=1, audio=pcm).event())
        except Exception:
            await self.close()
            return None
        hit = None
        while not self._hits.empty():
            hit = self._hits.get_nowait()
        return hit

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass
        if self._reader is not None and self._reader is not asyncio.current_task():
            self._reader.cancel()
        self._reader = None


class WakeDetector:
    """Per-satellite wrapper that follows the settings (model/backend can change live)."""

    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings
        self._impl: LocalWake | WyomingWake | None = None
        self._key: tuple[str, str, str] | None = None
        self.error: str | None = None
        self._failed_key: tuple[str, str, str] | None = None
        self._failed_at = 0.0

    @property
    def last_score(self) -> float:
        return self._impl.last_score if self._impl else 0.0

    def peak(self, window_s: float) -> float | None:
        """The model's best score over the last seconds (None: not known, e.g. Wyoming)."""
        return self._impl.peak(window_s) if isinstance(self._impl, LocalWake) else None

    async def _ensure(self) -> None:
        s = self.settings.get()
        key = (s.wake_backend, s.wake_word, s.wake_uri)
        if key == self._key:
            return
        if key == self._failed_key and time.monotonic() - self._failed_at < RETRY_FAILED_S:
            return
        await self.close()
        if s.wake_word == NO_MODEL:
            self._key = key
            self.error = None
            return
        try:
            if s.wake_backend == "wyoming":
                self._impl = WyomingWake(s.wake_uri, s.wake_word)
            else:
                self._impl = await asyncio.to_thread(LocalWake, s.wake_word)
            self._key = key
            self.error = None
            self._failed_key = None
            _LOGGER.info("Wake word '%s' via %s", s.wake_word, s.wake_backend)
        except Exception as err:
            _LOGGER.exception("Wake word '%s' failed to load", s.wake_word)
            self.error = f"{type(err).__name__}: {err}"
            self._failed_key = key
            self._failed_at = time.monotonic()

    async def feed(self, pcm: bytes) -> float | None:
        await self._ensure()
        impl = self._impl
        if impl is None:
            return None
        if isinstance(impl, WyomingWake):
            return await impl.feed(pcm)
        return await asyncio.to_thread(impl.process, pcm, self.settings.get().wake_threshold)

    def reset(self) -> None:
        if isinstance(self._impl, LocalWake):
            self._impl.reset()

    async def close(self) -> None:
        impl, self._impl, self._key = self._impl, None, None
        if isinstance(impl, WyomingWake):
            await impl.close()

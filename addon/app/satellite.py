"""Satellite link: one TCP connection per board on :10500.

Up:   microphone PCM, hello, events (tts_done, button:2, listen:on/off)
Down: TTS PCM, ring state, volume

Everything is decided on the PC, per board:
  audio -> VAD -> speaker id + GigaAM -> room diary (always)
        -> openWakeWord -> command capture -> GigaAM -> assistant -> Kokoro -> board

Frame header (16 bytes, big endian): b"HV02", type, channels, bits, 0,
rate (u32), payload bytes (u32). Old room_diary firmware sends b"HV01" +
rate + nbytes + mono PCM; it still feeds the diary but cannot speak.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections import deque
from typing import Any

from audio_utils import TARGET_RATE, duration_ms, normalize_pcm, rms
from settings import SettingsStore
from stt_client import transcribe
from tts_client import synthesize_stream
from tts_engine import resolve_voice
from wake import WakeDetector, strip_wake_word

try:
    import webrtcvad  # type: ignore
except Exception:  # pragma: no cover - optional
    webrtcvad = None

_LOGGER = logging.getLogger("ha-voice.satellite")

MAGIC_V1 = b"HV01"
MAGIC_V2 = b"HV02"
MAX_FRAME_BYTES = 256 * 1024

T_AUDIO = 0x01
T_HELLO = 0x02
T_EVENT = 0x03
T_PING = 0x04
T_TTS_START = 0x10
T_TTS_END = 0x11
T_STATE = 0x12
T_VOLUME = 0x13
T_TTS_ABORT = 0x14

ST_IDLE = 0
ST_WAKE = 1
ST_LISTENING = 2
ST_THINKING = 3
ST_REPLYING = 4
ST_ERROR = 5
STATE_NAMES = {
    ST_IDLE: "idle",
    ST_WAKE: "wake",
    ST_LISTENING: "listening",
    ST_THINKING: "thinking",
    ST_REPLYING: "replying",
    ST_ERROR: "error",
}

FRAME_BYTES = 640  # 20 ms at 16 kHz mono
TTS_CHUNK = 4096
PING_EVERY_S = 5.0
SILENT_LINK_S = 30.0
COMMAND_WAIT_S = 5.0


def pack(ftype: int, payload: bytes = b"", rate: int = 0, channels: int = 0, bits: int = 0) -> bytes:
    return MAGIC_V2 + struct.pack(">BBBBII", ftype, channels, bits, 0, rate, len(payload)) + payload


class Segmenter:
    """Server-side VAD: energy on 20 ms frames, confirmed by webrtcvad when installed."""

    def __init__(self, preroll_ms: int = 300) -> None:
        self.pre: deque[bytes] = deque(maxlen=max(1, preroll_ms // 20))
        self.buf = bytearray()
        self.in_speech = False
        self.silence_ms = 0
        self._rest = b""
        self._vad = webrtcvad.Vad(1) if webrtcvad is not None else None

    def _is_speech(self, frame: bytes, threshold: float) -> bool:
        if rms(frame) < threshold:
            return False
        if self._vad is not None:
            try:
                return self._vad.is_speech(frame, TARGET_RATE)
            except Exception:
                return True
        return True

    def feed(self, pcm: bytes, threshold: float, silence_ms: int, max_ms: int) -> list[bytes]:
        out: list[bytes] = []
        data = self._rest + pcm
        usable = len(data) - len(data) % FRAME_BYTES
        self._rest = data[usable:]
        max_bytes = max_ms * TARGET_RATE * 2 // 1000
        for i in range(0, usable, FRAME_BYTES):
            frame = data[i : i + FRAME_BYTES]
            if self._is_speech(frame, threshold):
                if not self.in_speech:
                    self.in_speech = True
                    self.buf = bytearray(b"".join(self.pre))
                    self.pre.clear()
                self.buf.extend(frame)
                self.silence_ms = 0
            elif self.in_speech:
                self.buf.extend(frame)
                self.silence_ms += 20
                if self.silence_ms >= silence_ms:
                    out.append(self._take())
                    continue
            else:
                self.pre.append(frame)
                continue
            if len(self.buf) >= max_bytes:
                out.append(self._take())
        return out

    def _take(self) -> bytes:
        seg = bytes(self.buf)
        self.buf = bytearray()
        self.in_speech = False
        self.silence_ms = 0
        return seg

    def flush(self) -> bytes | None:
        if self.in_speech and self.buf:
            return self._take()
        self.reset()
        return None

    def reset(self) -> None:
        self.buf = bytearray()
        self.in_speech = False
        self.silence_ms = 0
        self.pre.clear()
        self._rest = b""


class CommandCapture:
    """After the wake word (or the middle key): wait for speech, record until a pause."""

    TIMEOUT = object()

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.seg = Segmenter(preroll_ms=200)
        self.started = time.monotonic()

    def feed(self, pcm: bytes, threshold: float, silence_ms: int, max_ms: int) -> Any:
        done = self.seg.feed(pcm, threshold, silence_ms, max_ms)
        if done:
            return done[0]
        if not self.seg.in_speech and time.monotonic() - self.started > COMMAND_WAIT_S:
            return self.TIMEOUT
        return None


class Satellite:
    def __init__(self, hub: "SatelliteHub", writer: asyncio.StreamWriter, peer: Any) -> None:
        self.hub = hub
        self.writer = writer
        self.peer = peer
        self.ip = peer[0] if isinstance(peer, tuple) else str(peer)
        self.id = self.ip
        self.info: dict[str, str] = {}
        self.proto = 1
        self.connected_at = time.time()
        self.last_audio_at: float | None = None
        self.live_rms = 0.0
        self.state = ST_IDLE
        self.listening = True
        self.speaking = False
        self.busy = False
        self.mute_until = 0.0
        self.command: CommandCapture | None = None
        self.last_wake: dict[str, Any] | None = None
        self.diary_seg = Segmenter()
        self.wake = WakeDetector(hub.settings)
        self._tts_done = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._last_send = time.monotonic()
        self._speak_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    # ------------------------------------------------------------ status
    @property
    def can_speak(self) -> bool:
        return self.proto >= 2 and self.info.get("speaker", "1") == "1"

    def to_dict(self) -> dict[str, Any]:
        age = round(time.time() - self.last_audio_at, 1) if self.last_audio_at else None
        return {
            "id": self.id,
            "name": self.info.get("friendly_name") or self.id,
            "ip": self.info.get("ip") or self.ip,
            "mac": self.info.get("mac"),
            "esphome": self.info.get("esphome"),
            "proto": self.proto,
            "can_speak": self.can_speak,
            "listening": self.listening,
            "state": STATE_NAMES.get(self.state, str(self.state)),
            "live_rms": round(self.live_rms, 4),
            "last_audio_age_s": age,
            "connected_s": int(time.time() - self.connected_at),
            "wake_score": round(self.wake.last_score, 3),
            "wake_error": self.wake.error,
            "last_wake": self.last_wake,
            "mic_rate": self.info.get("mic_rate"),
            "mic_channels": self.info.get("mic_channels"),
        }

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------- down
    async def send(self, ftype: int, payload: bytes = b"", rate: int = 0, channels: int = 0, bits: int = 0) -> bool:
        if self.proto < 2 or self._closed:
            return False
        async with self._send_lock:
            try:
                self.writer.write(pack(ftype, payload, rate, channels, bits))
                await self.writer.drain()
            except (ConnectionError, RuntimeError):
                return False
            self._last_send = time.monotonic()
        return True

    async def set_state(self, state: int) -> None:
        self.state = state
        await self.send(T_STATE, bytes([state]))

    async def set_volume(self, percent: int) -> None:
        await self.send(T_VOLUME, bytes([max(0, min(100, int(percent)))]))

    async def ping_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(1.0)
            if self.proto >= 2 and time.monotonic() - self._last_send > PING_EVERY_S:
                await self.send(T_PING)

    # --------------------------------------------------------------- up
    async def on_hello(self, text: str) -> None:
        info = {}
        for line in text.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                info[k.strip()] = v.strip()
        self.info = info
        self.proto = 2
        self.listening = info.get("listening", "1") == "1"
        old_id = self.id
        self.id = info.get("name") or self.ip
        self.hub.rename(self, old_id)
        _LOGGER.info(
            "Satellite %s (%s) esphome %s mic %s Hz x%s",
            self.id, info.get("ip") or self.ip, info.get("esphome"), info.get("mic_rate"), info.get("mic_channels"),
        )
        await self.set_volume(self.hub.settings.get().sat_volume)
        await self.set_state(ST_IDLE)

    async def on_event(self, text: str) -> None:
        _LOGGER.debug("event %s: %s", self.id, text)
        if text == "tts_done":
            self._tts_done.set()
        elif text == "listen:on":
            self.listening = True
        elif text == "listen:off":
            self.listening = False
            self.diary_seg.reset()
            self.command = None
        elif text.startswith("button:2"):
            if self.speaking:
                await self.stop_speaking()
                await self.set_state(ST_IDLE)
            elif self.command is not None:
                self.command = None
                await self.set_state(ST_IDLE)
            elif not self.busy:
                await self.start_command("button")

    async def on_audio(self, pcm: bytes, real: bool = True) -> None:
        if real:
            self.last_audio_at = time.time()
            self.live_rms = rms(pcm)
            self.hub.runtime.feed_capture(pcm)
        # the board hears its own reply: ignore the mic while speaking
        if self.speaking or time.monotonic() < self.mute_until:
            return
        s = self.hub.settings.get()
        if s.diary_enabled:
            for seg in self.diary_seg.feed(pcm, s.diary_vad_rms, s.diary_silence_ms, s.diary_max_utterance_ms):
                self.hub.runtime.submit(seg, source=self.id)

        if self.command is not None:
            res = self.command.feed(pcm, s.diary_vad_rms, s.command_silence_ms, s.command_max_ms)
            if res is CommandCapture.TIMEOUT:
                _LOGGER.info("%s: no command after %s", self.id, self.command.reason)
                self.command = None
                await self.set_state(ST_IDLE)
            elif res:
                self.command = None
                self._spawn(self.run_command(res))
            return

        if self.busy:
            return
        score = await self.wake.feed(pcm)
        if score is not None:
            self.last_wake = {"ts": time.time(), "score": round(score, 3), "word": s.wake_word}
            _LOGGER.info("%s: wake word '%s' (%.2f)", self.id, s.wake_word, score)
            await self.start_command("wake")

    async def on_silence(self, ms: int) -> None:
        """Old firmware skips quiet frames: treat a read timeout as silence."""
        self.live_rms = 0.0
        await self.on_audio(b"\0" * (ms * TARGET_RATE * 2 // 1000), real=False)

    # ---------------------------------------------------------- dialogue
    async def start_command(self, reason: str) -> None:
        self.command = CommandCapture(reason)
        await self.set_state(ST_LISTENING)

    async def run_command(self, pcm: bytes) -> None:
        hub = self.hub
        s = hub.settings.get()
        self.busy = True
        entry: dict[str, Any] = {"satellite": self.id, "duration_ms": duration_ms(pcm)}
        try:
            await self.set_state(ST_THINKING)
            match_task = asyncio.create_task(asyncio.to_thread(hub.runtime.store.match, pcm))
            raw = await transcribe(hub.stt_uri, pcm)
            match = await match_task
            text = strip_wake_word(raw, s.wake_word)
            speaker = match.name if match.matched else None
            entry.update(text=text, heard=raw, speaker=speaker, score=round(float(match.score), 3))
            if not text:
                entry["error"] = "Пустое распознавание"
                _LOGGER.info("%s: command STT empty (%d ms)", self.id, entry["duration_ms"])
                hub.assistant.record(entry)
                await self.set_state(ST_ERROR)
                return
            if s.gate_require_match and hub.runtime.store.has_speakers() and not match.matched:
                entry.update(rejected=True, error="Голос не узнан — команда отклонена")
                _LOGGER.info("%s: command rejected, unknown voice (%.2f): %s", self.id, match.score, text)
                hub.assistant.record(entry)
                await self.set_state(ST_ERROR)
                return
            result = await hub.assistant.handle(text, conv_key=self.id, speaker=speaker)
            entry.update(
                reply=result.speech,
                backend=result.backend,
                actions=result.actions,
                expect_reply=result.expect_reply,
                error=result.error,
            )
            hub.assistant.record(entry)
            _LOGGER.info("%s: «%s» -> «%s»", self.id, text, result.speech)
            if result.speech and self.can_speak:
                self.say(result.speech, expect_reply=result.expect_reply)
            else:
                await self.set_state(ST_ERROR if result.error else ST_IDLE)
        except Exception as err:
            _LOGGER.exception("%s: command failed", self.id)
            entry["error"] = str(err)
            hub.assistant.record(entry)
            await self.set_state(ST_ERROR)
        finally:
            self.busy = False

    def say(self, text: str, voice: str | None = None, expect_reply: bool = False) -> asyncio.Task:
        if self._speak_task is not None and not self._speak_task.done():
            self._speak_task.cancel()
        self._speak_task = self._spawn(self._speak(text, voice, expect_reply))
        return self._speak_task

    async def stop_speaking(self) -> None:
        if self._speak_task is not None and not self._speak_task.done():
            self._speak_task.cancel()
        await self.send(T_TTS_ABORT)

    async def _speak(self, text: str, voice: str | None, expect_reply: bool) -> None:
        hub = self.hub
        voice = resolve_voice(voice or hub.settings.get().tts_voice)
        self.speaking = True
        self.command = None
        self._tts_done.clear()
        total = 0
        rate = 24000
        try:
            await self.set_state(ST_REPLYING)
            started = False
            async for r, pcm in synthesize_stream(hub.tts_uri, text, voice):
                if not started:
                    rate = r
                    await self.send(T_TTS_START, rate=rate, channels=1, bits=16)
                    started = True
                for i in range(0, len(pcm), TTS_CHUNK):
                    await self.send(T_AUDIO, pcm[i : i + TTS_CHUNK], rate=rate, channels=1, bits=16)
                total += len(pcm)
            await self.send(T_TTS_END)
            try:
                await asyncio.wait_for(self._tts_done.wait(), timeout=total / 2 / rate + 5)
            except asyncio.TimeoutError:
                _LOGGER.debug("%s: no tts_done from board", self.id)
        except asyncio.CancelledError:
            await self.send(T_TTS_ABORT)
            raise
        except Exception:
            _LOGGER.exception("%s: TTS to board failed", self.id)
            expect_reply = False
            await self.set_state(ST_ERROR)
            return
        finally:
            self.speaking = False
            self.mute_until = time.monotonic() + 0.4
            self.diary_seg.reset()
            self.wake.reset()
        if expect_reply:
            await self.start_command("follow_up")
        else:
            await self.set_state(ST_IDLE)

    async def close(self) -> None:
        self._closed = True
        for task in list(self._tasks):
            task.cancel()
        seg = self.diary_seg.flush()
        if seg and self.hub.settings.get().diary_enabled:
            self.hub.runtime.submit(seg, source=self.id)
        await self.wake.close()
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


class SatelliteHub:
    def __init__(self, settings: SettingsStore, runtime: Any, assistant: Any, stt_uri: str, tts_uri: str) -> None:
        self.settings = settings
        self.runtime = runtime
        self.assistant = assistant
        self.stt_uri = stt_uri
        self.tts_uri = tts_uri
        self.satellites: dict[str, Satellite] = {}

    def list(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.satellites.values()]

    def get(self, sat_id: str | None = None) -> Satellite | None:
        if sat_id:
            return self.satellites.get(sat_id)
        speaking = [s for s in self.satellites.values() if s.can_speak]
        return speaking[0] if speaking else next(iter(self.satellites.values()), None)

    def _count(self) -> None:
        self.runtime.pcm_clients = len(self.satellites)

    def rename(self, sat: Satellite, old_id: str) -> None:
        if self.satellites.get(old_id) is sat:
            del self.satellites[old_id]
        prev = self.satellites.get(sat.id)
        if prev is not None and prev is not sat:
            _LOGGER.info("Satellite %s reconnected, dropping old link", sat.id)
            asyncio.create_task(prev.close())
        self.satellites[sat.id] = sat
        self._count()

    async def set_volume(self, percent: int) -> None:
        for sat in list(self.satellites.values()):
            await sat.set_volume(percent)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        sat = Satellite(self, writer, peer)
        self.satellites[sat.id] = sat
        self._count()
        _LOGGER.info("Satellite connected %s", peer)
        pinger = asyncio.create_task(sat.ping_loop())
        try:
            while True:
                silence_s = self.settings.get().diary_silence_ms / 1000.0
                timeout = SILENT_LINK_S if sat.proto >= 2 else silence_s
                try:
                    magic = await asyncio.wait_for(reader.readexactly(4), timeout)
                except asyncio.TimeoutError:
                    if sat.proto >= 2:
                        _LOGGER.warning("Satellite %s silent for %ss, closing", sat.id, int(timeout))
                        break
                    await sat.on_silence(int(silence_s * 1000))
                    continue
                if magic == MAGIC_V1:
                    rate, nbytes = struct.unpack(">II", await reader.readexactly(8))
                    ftype, channels, bits = T_AUDIO, 1, 16
                elif magic == MAGIC_V2:
                    ftype, channels, bits, _, rate, nbytes = struct.unpack(">BBBBII", await reader.readexactly(12))
                else:
                    _LOGGER.warning("Bad frame magic %r from %s", magic, peer)
                    break
                if nbytes > MAX_FRAME_BYTES:
                    _LOGGER.warning("Frame too large from %s: %s bytes", peer, nbytes)
                    break
                payload = await reader.readexactly(nbytes) if nbytes else b""
                if ftype == T_AUDIO:
                    if not 8000 <= rate <= 48000 or bits not in (0, 16):
                        _LOGGER.warning("Unsupported audio from %s: rate=%s bits=%s", peer, rate, bits)
                        break
                    channels = channels or 1
                    pcm = payload if rate == TARGET_RATE and channels == 1 else normalize_pcm(payload, rate, channels)
                    await sat.on_audio(pcm)
                elif ftype == T_HELLO:
                    await sat.on_hello(payload.decode("utf-8", "replace"))
                elif ftype == T_EVENT:
                    await sat.on_event(payload.decode("utf-8", "replace").strip())
        except asyncio.IncompleteReadError:
            pass
        except Exception:
            _LOGGER.exception("Satellite %s error", sat.id)
        finally:
            pinger.cancel()
            if self.satellites.get(sat.id) is sat:
                del self.satellites[sat.id]
            self._count()
            await sat.close()
            _LOGGER.info("Satellite disconnected %s (%s)", sat.id, peer)

    async def serve(self, host: str, port: int) -> None:
        self.runtime.start()
        server = await asyncio.start_server(self.handle_client, host=host, port=port)
        _LOGGER.info("Satellite server on %s:%s", host, port)
        async with server:
            await server.serve_forever()

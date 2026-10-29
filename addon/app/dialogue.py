"""Continuous room-diary: PCM TCP ingest → VAD → speaker match → Wyoming STT → store."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient

from speakers import SpeakerStore

_LOGGER = logging.getLogger("ha-voice.dialogue")

# Frame header: magic(4) 'HV01' + rate(u32) + samples(u32) + pcm bytes
MAGIC = b"HV01"


class DialogueStore:
    def __init__(self, db_path: Path, jsonl_dir: Path) -> None:
        self.db_path = Path(db_path)
        self.jsonl_dir = Path(jsonl_dir)
        self.jsonl_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS utterances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    speaker_id TEXT,
                    speaker_name TEXT,
                    score REAL,
                    text TEXT NOT NULL,
                    duration_ms INTEGER
                )
                """
            )
            conn.commit()

    def add(
        self,
        text: str,
        speaker_id: str | None,
        speaker_name: str | None,
        score: float,
        duration_ms: int,
    ) -> dict[str, Any]:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        row = {
            "ts": ts,
            "speaker_id": speaker_id,
            "speaker_name": speaker_name or "unknown",
            "score": score,
            "text": text,
            "duration_ms": duration_ms,
        }
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO utterances (ts, speaker_id, speaker_name, score, text, duration_ms) VALUES (?,?,?,?,?,?)",
                (
                    row["ts"],
                    row["speaker_id"],
                    row["speaker_name"],
                    row["score"],
                    row["text"],
                    row["duration_ms"],
                ),
            )
            row["id"] = int(cur.lastrowid)
            conn.commit()
        day = ts[:10]
        with (self.jsonl_dir / f"{day}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def list_utterances(self, limit: int = 100, since: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT id, ts, speaker_id, speaker_name, score, text, duration_ms FROM utterances"
        args: list[Any] = []
        if since:
            q += " WHERE ts >= ?"
            args.append(since)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]


class DialogueRuntime:
    def __init__(
        self,
        store: SpeakerStore,
        dialogue_store: DialogueStore,
        stt_uri: str,
        sample_rate: int = 16000,
        vad_rms: float = 0.012,
        min_speech_ms: int = 400,
        max_utterance_ms: int = 15000,
        silence_ms: int = 700,
    ) -> None:
        self.store = store
        self.dialogue_store = dialogue_store
        self.stt_uri = stt_uri
        self.sample_rate = sample_rate
        self.vad_rms = vad_rms
        self.min_speech_ms = min_speech_ms
        self.max_utterance_ms = max_utterance_ms
        self.silence_ms = silence_ms
        self.enabled = True
        self.last_utterance: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    async def process_utterance(self, pcm: bytes, sample_rate: int | None = None) -> dict[str, Any] | None:
        rate = sample_rate or self.sample_rate
        if not self.enabled or not pcm:
            return None
        duration_ms = int(len(pcm) / 2 / rate * 1000)
        if duration_ms < self.min_speech_ms:
            return None

        async with self._lock:
            match = self.store.match_pcm(pcm, rate)
            text = await self._transcribe(pcm, rate)
            if not text.strip():
                return None
            name = match.name if match.matched else "unknown"
            sid = match.speaker_id if match.matched else None
            row = self.dialogue_store.add(
                text=text.strip(),
                speaker_id=sid,
                speaker_name=name,
                score=float(match.score),
                duration_ms=duration_ms,
            )
            self.last_utterance = row
            _LOGGER.info("diary [%s] %s", name, text.strip())
            return row

    async def _transcribe(self, pcm: bytes, rate: int) -> str:
        try:
            async with AsyncClient.from_uri(self.stt_uri) as client:
                await client.write_event(AudioStart(rate=rate, width=2, channels=1).event())
                step = rate * 2 // 5
                for i in range(0, len(pcm), max(step, 3200)):
                    await client.write_event(
                        AudioChunk(
                            rate=rate,
                            width=2,
                            channels=1,
                            audio=pcm[i : i + max(step, 3200)],
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
            _LOGGER.exception("Diary STT failed")
        return ""


def _rms_int16(pcm: bytes) -> float:
    import numpy as np

    if len(pcm) < 2:
        return 0.0
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return float((arr * arr).mean() ** 0.5)


class PcmSession:
    """Accumulate speech by energy VAD from framed PCM stream."""

    def __init__(self, runtime: DialogueRuntime) -> None:
        self.runtime = runtime
        self.buf = bytearray()
        self.in_speech = False
        self.silence_ms = 0
        self.speech_ms = 0
        self.rate = runtime.sample_rate

    async def feed(self, pcm: bytes, rate: int) -> None:
        self.rate = rate
        frame_ms = int(len(pcm) / 2 / rate * 1000) if rate else 0
        rms = _rms_int16(pcm)
        speaking = rms >= self.runtime.vad_rms

        if speaking:
            if not self.in_speech:
                self.in_speech = True
                self.buf = bytearray()
                self.silence_ms = 0
                self.speech_ms = 0
            self.buf.extend(pcm)
            self.speech_ms += frame_ms
            self.silence_ms = 0
            if self.speech_ms >= self.runtime.max_utterance_ms:
                await self._flush()
            return

        if self.in_speech:
            self.buf.extend(pcm)
            self.silence_ms += frame_ms
            if self.silence_ms >= self.runtime.silence_ms:
                await self._flush()

    async def _flush(self) -> None:
        pcm = bytes(self.buf)
        self.buf = bytearray()
        self.in_speech = False
        self.silence_ms = 0
        self.speech_ms = 0
        await self.runtime.process_utterance(pcm, self.rate)


async def handle_pcm_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    runtime: DialogueRuntime,
) -> None:
    peer = writer.get_extra_info("peername")
    _LOGGER.info("PCM client connected %s", peer)
    session = PcmSession(runtime)
    try:
        while True:
            hdr = await reader.readexactly(12)
            if hdr[:4] != MAGIC:
                _LOGGER.warning("Bad PCM magic from %s", peer)
                break
            rate, nbytes = struct.unpack(">II", hdr[4:12])
            if nbytes <= 0 or nbytes > 32000 * 2:
                break
            pcm = await reader.readexactly(nbytes)
            if runtime.enabled:
                await session.feed(pcm, rate)
    except asyncio.IncompleteReadError:
        pass
    except Exception:
        _LOGGER.exception("PCM client error")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        _LOGGER.info("PCM client disconnected %s", peer)


async def run_pcm_server(host: str, port: int, runtime: DialogueRuntime) -> None:
    server = await asyncio.start_server(
        lambda r, w: handle_pcm_client(r, w, runtime),
        host=host,
        port=port,
    )
    _LOGGER.info("Diary PCM server on %s:%s", host, port)
    async with server:
        await server.serve_forever()


def build_runtime_from_env(store: SpeakerStore) -> DialogueRuntime:
    dialogues_dir = Path(os.environ.get("DIALOGUES_DIR", "/data/dialogues"))
    db = Path(os.environ.get("DIALOGUE_DB", str(dialogues_dir / "dialogue.db")))
    return DialogueRuntime(
        store=store,
        dialogue_store=DialogueStore(db, dialogues_dir),
        stt_uri=os.environ.get("DIALOGUE_STT_URI", "tcp://127.0.0.1:10305"),
        sample_rate=int(os.environ.get("DIALOGUE_SAMPLE_RATE", "16000")),
        vad_rms=float(os.environ.get("DIALOGUE_VAD_RMS", "0.012")),
        min_speech_ms=int(os.environ.get("DIALOGUE_MIN_SPEECH_MS", "400")),
        max_utterance_ms=int(os.environ.get("DIALOGUE_MAX_UTTERANCE_MS", "15000")),
        silence_ms=int(os.environ.get("DIALOGUE_SILENCE_MS", "700")),
    )

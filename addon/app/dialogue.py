"""Room diary: utterances (cut by the satellite VAD) → speaker match → STT → SQLite/JSONL (+ audio clips)."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audio_utils import TARGET_RATE, duration_ms, to_wav
from settings import SettingsStore
from speakers import SpeakerStore
from stt_client import transcribe

_LOGGER = logging.getLogger("ha-voice.dialogue")

UNKNOWN = "__unknown__"

_COLUMNS = "id, ts, speaker_id, speaker_name, score, text, duration_ms, has_audio, source"


class DialogueStore:
    def __init__(self, db_path: Path, root_dir: Path) -> None:
        self.db_path = Path(db_path)
        self.root_dir = Path(root_dir)
        self.audio_dir = self.root_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
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
            cols = {row[1] for row in conn.execute("PRAGMA table_info(utterances)")}
            if "has_audio" not in cols:
                conn.execute("ALTER TABLE utterances ADD COLUMN has_audio INTEGER NOT NULL DEFAULT 0")
            if "source" not in cols:
                conn.execute("ALTER TABLE utterances ADD COLUMN source TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_utt_ts ON utterances(ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_utt_speaker ON utterances(speaker_id)")

    def audio_path(self, utt_id: int) -> Path:
        return self.audio_dir / f"{int(utt_id)}.wav"

    def add(
        self,
        text: str,
        speaker_id: str | None,
        speaker_name: str | None,
        score: float,
        duration: int,
        pcm: bytes | None,
        source: str | None = None,
    ) -> dict[str, Any]:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO utterances (ts, speaker_id, speaker_name, score, text, duration_ms, has_audio, source)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (ts, speaker_id, speaker_name or "unknown", score, text, duration, 1 if pcm else 0, source),
            )
            utt_id = int(cur.lastrowid)
        if pcm:
            self.audio_path(utt_id).write_bytes(to_wav(pcm))
        row = {
            "id": utt_id,
            "ts": ts,
            "speaker_id": speaker_id,
            "speaker_name": speaker_name or "unknown",
            "score": score,
            "text": text,
            "duration_ms": duration,
            "has_audio": bool(pcm),
            "source": source,
        }
        with (self.root_dir / f"{ts[:10]}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({k: v for k, v in row.items() if k != "has_audio"}, ensure_ascii=False) + "\n")
        return row

    @staticmethod
    def _where(
        speaker: str | None, q: str | None, since: str | None, until: str | None
    ) -> tuple[str, list[Any]]:
        parts: list[str] = []
        args: list[Any] = []
        if speaker == UNKNOWN:
            parts.append("speaker_id IS NULL")
        elif speaker:
            parts.append("speaker_id = ?")
            args.append(speaker)
        if q:
            parts.append("text LIKE ?")
            args.append(f"%{q}%")
        if since:
            parts.append("ts >= ?")
            args.append(since)
        if until:
            parts.append("ts < ?")
            args.append(until)
        return (" WHERE " + " AND ".join(parts)) if parts else "", args

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["has_audio"] = bool(d.get("has_audio"))
        return d

    def query(
        self,
        limit: int = 50,
        offset: int = 0,
        speaker: str | None = None,
        q: str | None = None,
        since: str | None = None,
        until: str | None = None,
        ascending: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        where, args = self._where(speaker, q, since, until)
        order = "ASC" if ascending else "DESC"
        with closing(self._connect()) as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM utterances{where}", args).fetchone()[0])
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM utterances{where} ORDER BY id {order} LIMIT ? OFFSET ?",
                [*args, int(limit), int(offset)],
            ).fetchall()
        return [self._row(r) for r in rows], total

    def get(self, utt_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            r = conn.execute(f"SELECT {_COLUMNS} FROM utterances WHERE id = ?", (int(utt_id),)).fetchone()
        return self._row(r) if r else None

    def set_speaker(self, utt_id: int, speaker_id: str | None, speaker_name: str | None) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE utterances SET speaker_id = ?, speaker_name = ?, score = NULL WHERE id = ?",
                (speaker_id, speaker_name or "unknown", int(utt_id)),
            )

    def rename_speaker(self, speaker_id: str, name: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE utterances SET speaker_name = ? WHERE speaker_id = ?", (name, speaker_id))

    def forget_speaker(self, speaker_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE utterances SET speaker_id = NULL, speaker_name = 'unknown' WHERE speaker_id = ?",
                (speaker_id,),
            )

    def delete(self, utt_id: int) -> bool:
        with closing(self._connect()) as conn, conn:
            cur = conn.execute("DELETE FROM utterances WHERE id = ?", (int(utt_id),))
        self.audio_path(utt_id).unlink(missing_ok=True)
        return cur.rowcount > 0

    def prune_audio(self, keep: int) -> None:
        with closing(self._connect()) as conn, conn:
            ids = [
                int(r[0])
                for r in conn.execute(
                    "SELECT id FROM utterances WHERE has_audio = 1 ORDER BY id DESC LIMIT -1 OFFSET ?",
                    (max(0, int(keep)),),
                )
            ]
            if ids:
                conn.executemany("UPDATE utterances SET has_audio = 0 WHERE id = ?", [(i,) for i in ids])
        for i in ids:
            self.audio_path(i).unlink(missing_ok=True)

    def stats(self, since: str | None = None) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM utterances").fetchone()[0])
            where, args = self._where(None, None, since, None)
            period = int(conn.execute(f"SELECT COUNT(*) FROM utterances{where}", args).fetchone()[0])
            unknown_where, unknown_args = self._where(UNKNOWN, None, since, None)
            unknown = int(conn.execute(f"SELECT COUNT(*) FROM utterances{unknown_where}", unknown_args).fetchone()[0])
            by_speaker = [
                {"speaker_id": r[0], "count": int(r[1]), "last_ts": r[2]}
                for r in conn.execute(
                    "SELECT speaker_id, COUNT(*), MAX(ts) FROM utterances"
                    " WHERE speaker_id IS NOT NULL GROUP BY speaker_id"
                )
            ]
        return {"total": total, "period": period, "period_unknown": unknown, "by_speaker": by_speaker}


class DialogueRuntime:
    def __init__(
        self,
        store: SpeakerStore,
        dialogue_store: DialogueStore,
        settings: SettingsStore,
        stt_uri: str,
    ) -> None:
        self.store = store
        self.dialogue_store = dialogue_store
        self.settings = settings
        self.stt_uri = stt_uri
        self.last_utterance: dict[str, Any] | None = None
        self.pcm_clients = 0
        self.last_audio_at: float | None = None
        self.live_rms = 0.0
        self.processed = 0
        self.dropped = 0
        self.stt_empty = 0
        self.queue: asyncio.Queue[tuple[bytes, str | None]] | None = None
        self._worker_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._capture_until: float | None = None
        self._capture_buf = bytearray()
        self._capture_pcm: bytes | None = None
        self._capture_started_at: float | None = None

    @property
    def enabled(self) -> bool:
        return self.settings.get().diary_enabled

    def set_enabled(self, value: bool) -> None:
        self.settings.update({"diary_enabled": bool(value)})

    def start(self) -> None:
        if self.queue is None:
            self.queue = asyncio.Queue(maxsize=32)
            self._worker_task = asyncio.create_task(self._worker())

    def queued(self) -> int:
        return self.queue.qsize() if self.queue else 0

    def capture_start(self, seconds: float = 8.0) -> dict[str, Any]:
        if self.pcm_clients <= 0:
            raise ValueError("Колонка не подключена к HA Voice. Проверьте, что она в сети и включён «Слушать».")
        seconds = max(2.0, min(20.0, float(seconds)))
        now = time.time()
        self._capture_until = now + seconds
        self._capture_started_at = now
        self._capture_buf = bytearray()
        self._capture_pcm = None
        return self.capture_status()

    def capture_stop(self) -> dict[str, Any]:
        if self._capture_until is not None:
            self._finish_capture()
        return self.capture_status()

    def capture_clear(self) -> None:
        self._capture_until = None
        self._capture_buf = bytearray()
        self._capture_pcm = None
        self._capture_started_at = None

    def _finish_capture(self) -> None:
        self._capture_until = None
        pcm = bytes(self._capture_buf)
        self._capture_buf = bytearray()
        self._capture_pcm = pcm if len(pcm) >= TARGET_RATE * 2 else None  # ≥1 с

    def capture_status(self) -> dict[str, Any]:
        now = time.time()
        active = self._capture_until is not None and now < self._capture_until
        if self._capture_until is not None and not active:
            self._finish_capture()
        pcm = self._capture_pcm
        buf_ms = duration_ms(bytes(self._capture_buf)) if self._capture_buf else 0
        done_ms = duration_ms(pcm) if pcm else 0
        remaining = max(0.0, (self._capture_until or 0) - now) if self._capture_until else 0.0
        return {
            "active": active,
            "ready": bool(pcm),
            "remaining_s": round(remaining, 1),
            "duration_ms": done_ms if pcm else buf_ms,
            "pcm_clients": self.pcm_clients,
            "live_rms": round(self.live_rms, 4),
        }

    def capture_pcm(self) -> bytes | None:
        if self._capture_until is not None and time.time() >= self._capture_until:
            self._finish_capture()
        return self._capture_pcm

    def feed_capture(self, pcm: bytes) -> None:
        if self._capture_until is None:
            return
        now = time.time()
        if now >= self._capture_until:
            self._finish_capture()
            return
        # не раздувать буфер бесконечно
        if len(self._capture_buf) + len(pcm) <= TARGET_RATE * 2 * 25:
            self._capture_buf.extend(pcm)

    def submit(self, pcm: bytes, source: str | None = None) -> None:
        if self.queue is None:
            return
        try:
            self.queue.put_nowait((pcm, source))
        except asyncio.QueueFull:
            self.dropped += 1
            _LOGGER.warning("Diary queue full, utterance dropped")

    async def _worker(self) -> None:
        assert self.queue is not None
        while True:
            pcm, source = await self.queue.get()
            try:
                await self.process_utterance(pcm, source=source)
            except Exception:
                _LOGGER.exception("Diary processing failed")
            finally:
                self.queue.task_done()

    async def process_utterance(
        self, pcm: bytes, force: bool = False, source: str | None = None
    ) -> dict[str, Any] | None:
        """pcm: 16 kHz mono PCM16."""
        s = self.settings.get()
        if not pcm or (not force and not s.diary_enabled):
            return None
        dur = duration_ms(pcm)
        if dur < s.diary_min_speech_ms:
            return None

        async with self._lock:
            match = await asyncio.to_thread(self.store.match, pcm)
            text = await transcribe(self.stt_uri, pcm)
            if not text:
                self.stt_empty += 1
                _LOGGER.info(
                    "diary: STT empty for %d ms utterance from %s (voice %s %.2f) — not saved",
                    dur, source or "upload", match.name if match.matched else "unknown", match.score,
                )
                return None
            keep = s.diary_keep_audio
            row = self.dialogue_store.add(
                text=text,
                speaker_id=match.speaker_id if match.matched else None,
                speaker_name=match.name if match.matched else "unknown",
                score=round(float(match.score), 3),
                duration=dur,
                pcm=pcm if keep > 0 else None,
                source=source,
            )
            self.processed += 1
            if keep > 0 and self.processed % 20 == 0:
                await asyncio.to_thread(self.dialogue_store.prune_audio, keep)
            self.last_utterance = row
            _LOGGER.info("diary [%s %.2f] %s", row["speaker_name"], match.score, text)
            return row

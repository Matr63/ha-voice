"""Speaker enrollment and verification (Resemblyzer voice embeddings)."""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from audio_utils import TARGET_RATE, pcm16_to_float, to_wav
from settings import SettingsStore

_LOGGER = logging.getLogger("ha-voice.speakers")

_NAME_RE = re.compile(r"^[\w .()\-]{1,40}$")
MIN_ENROLL_SEC = 1.0
MIN_MATCH_SEC = 0.5
MAX_SAMPLES_KEPT = 10


@dataclass
class MatchResult:
    speaker_id: str | None
    name: str | None
    score: float
    matched: bool
    scores: list[dict[str, Any]] = field(default_factory=list)


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("Имя: 1–40 символов, буквы, цифры, пробел, . - ( )")
    return name


class SpeakerStore:
    def __init__(self, root: Path, settings: SettingsStore) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self._lock = threading.RLock()
        self._encoder_lock = threading.Lock()
        self._encoder = None
        self._embeddings: dict[str, np.ndarray] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._load()

    @property
    def threshold(self) -> float:
        return self.settings.get().gate_threshold

    def _speaker_dir(self, speaker_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-fA-F\-]{1,64}", speaker_id or ""):
            raise ValueError("Bad speaker id")
        return self.root / speaker_id

    def _load(self) -> None:
        with self._lock:
            self._embeddings.clear()
            self._meta.clear()
            for path in sorted(self.root.glob("*/meta.json")):
                try:
                    meta = json.loads(path.read_text(encoding="utf-8"))
                    emb_path = path.parent / "embedding.npy"
                    if not emb_path.is_file():
                        continue
                    sid = str(meta.get("id") or path.parent.name)
                    self._meta[sid] = meta
                    self._embeddings[sid] = np.load(emb_path).astype(np.float32)
                except Exception:
                    _LOGGER.exception("Failed to load speaker %s", path)

    def _save_meta(self, sid: str, meta: dict[str, Any]) -> None:
        d = self._speaker_dir(sid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def has_speakers(self) -> bool:
        with self._lock:
            return bool(self._embeddings)

    def get(self, speaker_id: str) -> dict[str, Any] | None:
        with self._lock:
            meta = self._meta.get(speaker_id)
            return self._public(speaker_id, meta) if meta else None

    @staticmethod
    def _public(sid: str, meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": sid,
            "name": meta.get("name", sid),
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            "samples": int(meta.get("samples", 0)),
            "seconds": round(float(meta.get("seconds", 0.0)), 1),
        }

    def list_speakers(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [self._public(sid, meta) for sid, meta in self._meta.items()]
        return sorted(items, key=lambda x: str(x["name"]).lower())

    def name_of(self, speaker_id: str | None) -> str | None:
        if not speaker_id:
            return None
        with self._lock:
            meta = self._meta.get(speaker_id)
            return str(meta.get("name")) if meta else None

    def rename(self, speaker_id: str, name: str) -> dict[str, Any]:
        name = validate_name(name)
        with self._lock:
            meta = self._meta.get(speaker_id)
            if not meta:
                raise KeyError(speaker_id)
            meta = {**meta, "name": name, "updated_at": _now()}
            self._save_meta(speaker_id, meta)
            self._meta[speaker_id] = meta
            return self._public(speaker_id, meta)

    def delete_speaker(self, speaker_id: str) -> bool:
        with self._lock:
            if speaker_id not in self._meta:
                return False
            shutil.rmtree(self._speaker_dir(speaker_id), ignore_errors=True)
            self._embeddings.pop(speaker_id, None)
            self._meta.pop(speaker_id, None)
            return True

    def _ensure_encoder(self):
        if self._encoder is None:
            from resemblyzer import VoiceEncoder

            _LOGGER.info("Loading Resemblyzer VoiceEncoder")
            self._encoder = VoiceEncoder(device="cpu")
        return self._encoder

    def embed(self, pcm: bytes) -> np.ndarray | None:
        """pcm: 16 kHz mono PCM16."""
        wav = pcm16_to_float(pcm)
        if wav.size < TARGET_RATE * MIN_MATCH_SEC:
            return None
        with self._encoder_lock:
            encoder = self._ensure_encoder()
            try:
                from resemblyzer import preprocess_wav

                processed = preprocess_wav(wav, source_sr=TARGET_RATE)
            except Exception:
                processed = wav
            if processed.size < TARGET_RATE * 0.3:
                processed = wav
            emb = np.asarray(encoder.embed_utterance(processed), dtype=np.float32)
        norm = float(np.linalg.norm(emb))
        return emb / norm if norm > 1e-9 else None

    def match(self, pcm: bytes) -> MatchResult:
        emb = self.embed(pcm)
        if emb is None:
            return MatchResult(None, None, 0.0, False)
        with self._lock:
            refs = list(self._embeddings.items())
            names = {sid: str(m.get("name") or sid) for sid, m in self._meta.items()}
        if not refs:
            return MatchResult(None, None, 0.0, False)
        scores = sorted(
            ({"id": sid, "name": names.get(sid, sid), "score": round(float(np.dot(emb, ref)), 3)} for sid, ref in refs),
            key=lambda x: x["score"],
            reverse=True,
        )
        best = scores[0]
        return MatchResult(best["id"], best["name"], best["score"], best["score"] >= self.threshold, scores)

    def enroll(self, name: str | None, pcm: bytes, speaker_id: str | None = None) -> dict[str, Any]:
        """Create a new speaker (name) or add a sample to an existing one (speaker_id)."""
        if len(pcm) < TARGET_RATE * 2 * MIN_ENROLL_SEC:
            raise ValueError(f"Слишком короткая запись — нужно хотя бы {MIN_ENROLL_SEC:.0f} с речи")
        emb = self.embed(pcm)
        if emb is None:
            raise ValueError("Не удалось выделить голос из записи")

        with self._lock:
            old = self._meta.get(speaker_id) if speaker_id else None
            if speaker_id and not old:
                raise KeyError(speaker_id)
            sid = speaker_id or str(uuid.uuid4())
            clean_name = validate_name(name) if name else str((old or {}).get("name") or "")
            if not clean_name:
                raise ValueError("Укажите имя")
            samples = int((old or {}).get("samples", 0)) + 1
            if old and sid in self._embeddings:
                prev = self._embeddings[sid]
                emb = prev * (samples - 1) + emb
                emb = (emb / max(float(np.linalg.norm(emb)), 1e-9)).astype(np.float32)

            now = _now()
            meta = {
                "id": sid,
                "name": clean_name,
                "created_at": (old or {}).get("created_at", now),
                "updated_at": now,
                "samples": samples,
                "seconds": float((old or {}).get("seconds", 0.0)) + len(pcm) / 2 / TARGET_RATE,
            }
            d = self._speaker_dir(sid)
            d.mkdir(parents=True, exist_ok=True)
            np.save(d / "embedding.npy", emb)
            self._save_meta(sid, meta)
            (d / f"sample_{samples:04d}.wav").write_bytes(to_wav(pcm[: TARGET_RATE * 2 * 30]))
            for stale in sorted(d.glob("sample_*.wav"))[:-MAX_SAMPLES_KEPT]:
                stale.unlink(missing_ok=True)
            self._embeddings[sid] = emb
            self._meta[sid] = meta
            return self._public(sid, meta)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

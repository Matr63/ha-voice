"""Speaker enrollment and verification (Resemblyzer voice embeddings)."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

_LOGGER = logging.getLogger("ha-voice.speakers")

_NAME_RE = re.compile(r"^[a-zA-Zа-яА-ЯёЁ0-9 _.\-]{1,64}$")


@dataclass
class MatchResult:
    speaker_id: str | None
    name: str | None
    score: float
    matched: bool


class SpeakerStore:
    def __init__(self, root: Path, threshold: float = 0.55) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.threshold = float(threshold)
        self._lock = threading.RLock()
        self._encoder = None
        self._embeddings: dict[str, np.ndarray] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._load()

    def _ensure_encoder(self):
        if self._encoder is not None:
            return self._encoder
        from resemblyzer import VoiceEncoder

        _LOGGER.info("Loading Resemblyzer VoiceEncoder")
        self._encoder = VoiceEncoder()
        return self._encoder

    def _speaker_dir(self, speaker_id: str) -> Path:
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
                    emb = np.load(emb_path)
                    sid = str(meta.get("id") or path.parent.name)
                    self._meta[sid] = meta
                    self._embeddings[sid] = emb.astype(np.float32)
                except Exception:
                    _LOGGER.exception("Failed to load speaker %s", path)

    def list_speakers(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for sid, meta in sorted(self._meta.items(), key=lambda x: x[1].get("name", x[0])):
                out.append(
                    {
                        "id": sid,
                        "name": meta.get("name", sid),
                        "created_at": meta.get("created_at"),
                        "updated_at": meta.get("updated_at"),
                        "samples": int(meta.get("samples", 0)),
                    }
                )
            return out

    def delete_speaker(self, speaker_id: str) -> bool:
        with self._lock:
            d = self._speaker_dir(speaker_id)
            if not d.exists():
                return False
            for p in d.glob("*"):
                p.unlink(missing_ok=True)
            d.rmdir()
            self._embeddings.pop(speaker_id, None)
            self._meta.pop(speaker_id, None)
            return True

    def _pcm16_to_float(self, pcm: bytes, sample_rate: int) -> np.ndarray:
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if sample_rate != 16000 and len(audio) > 0:
            # crude resample via linear interpolation
            duration = len(audio) / float(sample_rate)
            new_len = max(1, int(duration * 16000))
            x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=new_len, endpoint=False)
            audio = np.interp(x_new, x_old, audio).astype(np.float32)
        return audio

    def embed_pcm(self, pcm: bytes, sample_rate: int = 16000) -> np.ndarray | None:
        if not pcm or len(pcm) < 3200:  # <100ms @16k mono s16
            return None
        encoder = self._ensure_encoder()
        wav = self._pcm16_to_float(pcm, sample_rate)
        if wav.size < 1600:
            return None
        # Resemblyzer expects float waveform @ 16 kHz
        emb = encoder.embed_utterance(wav)
        return np.asarray(emb, dtype=np.float32)

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na <= 1e-9 or nb <= 1e-9:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    def match_pcm(self, pcm: bytes, sample_rate: int = 16000) -> MatchResult:
        emb = self.embed_pcm(pcm, sample_rate)
        if emb is None:
            return MatchResult(None, None, 0.0, False)
        with self._lock:
            if not self._embeddings:
                return MatchResult(None, None, 0.0, False)
            best_id = None
            best_score = -1.0
            for sid, ref in self._embeddings.items():
                score = self._cosine(emb, ref)
                if score > best_score:
                    best_score = score
                    best_id = sid
            matched = best_id is not None and best_score >= self.threshold
            name = None
            if best_id and best_id in self._meta:
                name = str(self._meta[best_id].get("name") or best_id)
            return MatchResult(best_id if matched else best_id, name if matched else name, best_score, matched)

    def enroll(
        self,
        name: str,
        pcm: bytes,
        sample_rate: int = 16000,
        speaker_id: str | None = None,
    ) -> dict[str, Any]:
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            raise ValueError("Invalid speaker name")
        emb = self.embed_pcm(pcm, sample_rate)
        if emb is None:
            raise ValueError("Audio too short or empty for enrollment")

        with self._lock:
            sid = speaker_id or str(uuid.uuid4())
            d = self._speaker_dir(sid)
            d.mkdir(parents=True, exist_ok=True)
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            old = self._meta.get(sid)
            samples = int(old.get("samples", 0)) + 1 if old else 1
            if old and sid in self._embeddings:
                # running average of embeddings
                prev = self._embeddings[sid]
                emb = ((prev * (samples - 1)) + emb) / float(samples)
                emb = emb.astype(np.float32)

            meta = {
                "id": sid,
                "name": name,
                "created_at": (old or {}).get("created_at", now),
                "updated_at": now,
                "samples": samples,
            }
            np.save(d / "embedding.npy", emb)
            (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            # keep last enrollment wav for debugging
            (d / f"sample_{samples}.raw").write_bytes(pcm[: 16000 * 2 * 10])
            self._embeddings[sid] = emb
            self._meta[sid] = meta
            return meta

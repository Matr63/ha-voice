"""Kokoro-ru ONNX engine. Weights live under HF_HOME and survive add-on updates."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download, snapshot_download

from audio_utils import float_to_pcm16

_LOGGER = logging.getLogger("ha-voice.tts")

REPO = "zaakirio/kokoro-ru"
SAMPLE_RATE = 24000
MAX_PHONEMES = 500
VOICES: dict[str, dict[str, str]] = {
    "sveta": {"label": "Света", "gender": "female", "onnx": "onnx/model_quantized.onnx"},
    "masha": {"label": "Маша", "gender": "female", "onnx": "onnx/model_quantized.onnx"},
    "dima": {"label": "Дима", "gender": "male", "onnx": "onnx/model_dima_quantized.onnx"},
}
DEFAULT_VOICE = "sveta"
_SENT_RE = re.compile(r"(?<=[.!?…;:])\s+|\n+")
_NEED = [
    "ru_g2p.py",
    "kokoro-config.json",
    "config.json",
    "onnx/model_quantized.onnx",
    "onnx/model_dima_quantized.onnx",
    "voices/sveta.bin",
    "voices/masha.bin",
    "voices/dima.bin",
]


def data_root() -> Path:
    base = Path(os.environ.get("HF_HOME", "/data/models"))
    return base / "kokoro-ru"


def resolve_voice(name: str | None) -> str:
    key = (name or "").strip().lower()
    if key in VOICES:
        return key
    return DEFAULT_VOICE


class KokoroEngine:
    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root else data_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._g2p: Any = None
        self._vocab: dict[str, int] = {}
        self._sessions: dict[str, ort.InferenceSession] = {}
        self._styles: dict[str, np.ndarray] = {}
        self.ready = False
        self.error: str | None = None

    def load(self) -> None:
        _LOGGER.info("Kokoro-ru: downloading assets to %s", self.root)
        self._ensure_files()
        self._load_g2p()
        for name in VOICES:
            self._styles[name] = np.fromfile(self.root / "voices" / f"{name}.bin", dtype=np.float32).reshape(510, 256)
        # preload default (sveta/masha share one onnx)
        self._session_for(DEFAULT_VOICE)
        self.ready = True
        self.error = None
        _LOGGER.info("Kokoro-ru ready, voices=%s", ", ".join(VOICES))

    def _ensure_files(self) -> None:
        for rel in _NEED:
            dest = self.root / rel
            if dest.is_file() and dest.stat().st_size > 0:
                continue
            _LOGGER.info("download %s", rel)
            hf_hub_download(REPO, rel, local_dir=str(self.root))
        ru_dict = self.root / "espeak-data" / "ru_dict"
        if not ru_dict.is_file():
            _LOGGER.info("download espeak-data/")
            snapshot_download(REPO, local_dir=str(self.root), allow_patterns=["espeak-data/**"])

    def _load_g2p(self) -> None:
        path = self.root / "ru_g2p.py"
        spec = importlib.util.spec_from_file_location("kokoro_ru_g2p", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("не удалось загрузить ru_g2p.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        g2p_cfg = self.root / "kokoro-config.json"
        self._g2p = mod.RuG2P(espeak_data=self.root / "espeak-data", vocab_path=g2p_cfg)
        onnx_cfg = self.root / "config.json"
        cfg = json.loads((onnx_cfg if onnx_cfg.is_file() else g2p_cfg).read_text(encoding="utf-8"))
        self._vocab = {str(k): int(v) for k, v in cfg.get("vocab", {}).items()}

    def _session_for(self, voice: str) -> ort.InferenceSession:
        onnx_rel = VOICES[voice]["onnx"]
        if onnx_rel in self._sessions:
            return self._sessions[onnx_rel]
        so = ort.SessionOptions()
        so.intra_op_num_threads = int(os.environ.get("TTS_THREADS", "2"))
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        path = str(self.root / onnx_rel)
        _LOGGER.info("ONNX session %s", onnx_rel)
        sess = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
        self._sessions[onnx_rel] = sess
        return sess

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        voice = resolve_voice(voice)
        text = " ".join((text or "").split())
        if not text:
            return b""
        with self._lock:
            parts: list[bytes] = []
            for chunk in _chunks(text):
                pcm = self._synth_one(chunk, voice)
                if pcm:
                    parts.append(pcm)
            return b"".join(parts)

    def _synth_one(self, text: str, voice: str) -> bytes:
        ipa, oov = self._g2p(text)
        if oov:
            _LOGGER.debug("OOV symbols %s in %r", oov, text[:80])
        ids = [self._vocab[c] for c in ipa if c in self._vocab]
        if not ids:
            return b""
        if len(ids) > MAX_PHONEMES:
            ids = ids[:MAX_PHONEMES]
            ipa = ipa[:MAX_PHONEMES]
        style = self._styles[voice]
        row = min(max(len(ipa) - 1, 0), style.shape[0] - 1)
        sess = self._session_for(voice)
        inputs = {
            "input_ids": np.array([[0, *ids, 0]], dtype=np.int64),
            "style": style[row][None].astype(np.float32),
            "speed": np.ones(1, dtype=np.float32),
        }
        audio = sess.run(None, inputs)[0]
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        return float_to_pcm16(audio)


def _chunks(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_RE.split(text) if p.strip()]
    if not parts:
        return [text]
    out: list[str] = []
    for p in parts:
        if len(p) <= 280:
            out.append(p)
            continue
        words = p.split()
        buf: list[str] = []
        n = 0
        for w in words:
            if n + len(w) + 1 > 280 and buf:
                out.append(" ".join(buf))
                buf = [w]
                n = len(w)
            else:
                buf.append(w)
                n += len(w) + 1
        if buf:
            out.append(" ".join(buf))
    return out

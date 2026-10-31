"""Kokoro-ru ONNX engine. Weights live under HF_HOME and survive add-on updates."""
from __future__ import annotations

import functools
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import threading
import time
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
# Small text files are re-checked on every start: ru_g2p.py follows library
# changes upstream (e.g. the transformers v5 shim), a stale copy breaks Kokoro.
_REFRESH = ["ru_g2p.py", "kokoro-config.json", "config.json"]
_NEED = [
    *_REFRESH,
    "onnx/model_quantized.onnx",
    "onnx/model_dima_quantized.onnx",
    "voices/sveta.bin",
    "voices/masha.bin",
    "voices/dima.bin",
]


def data_root() -> Path:
    base = Path(os.environ.get("HF_HOME", "/data/models"))
    return base / "kokoro-ru"


def state_path() -> Path:
    return Path(os.environ.get("TTS_STATE_FILE", "/tmp/ha-voice-logs/tts_state.json"))


def write_state(stage: str, error: str | None = None, **extra: Any) -> None:
    """Loading progress for the panel: the Wyoming port only opens once Kokoro is ready."""
    data = {"stage": stage, "error": error, "ts": time.time(), "pid": os.getpid(), **extra}
    try:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def read_state() -> dict[str, Any]:
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


RUACCENT_REPO = "ruaccent/accentuator"
RUACCENT_DONE = ".ha-voice-complete"
OMOGRAPH_MODEL = "turbo3.1"  # what ru_g2p.py asks for
REFRESH_EVERY_S = 7 * 24 * 3600


def ruaccent_patterns(omograph: str = OMOGRAPH_MODEL) -> list[str]:
    """The part of ruaccent/accentuator that RUAccent.load() reads."""
    return [
        "dictionary/*",
        "nn/nn_accent/*",
        "nn/nn_stress_usage_predictor/*",
        "nn/nn_yo_homograph_resolver/*",
        f"nn/nn_omograph/{omograph}/*",
        "koziev/*",
    ]


def ruaccent_dir(root: Path | None = None) -> Path:
    return (root or data_root()).parent / "ruaccent"


def _ruaccent_workdir(root: Path) -> None:
    """RUAccent downloads its models itself, next to the package (inside the
    image: gone after every add-on restart), and only checks that a folder
    exists, so a download cut in the middle stays broken forever. Here the
    models are fetched by us into /data with resume, marked complete, and
    RUAccent finds them all on disk: no network once they are there.
    koziev/ is always read next to the package, so it is a symlink."""
    try:
        from ruaccent import RUAccent
    except ImportError:
        return
    if getattr(RUAccent.load, "_ha_voice", False):
        return
    if "workdir" not in inspect.signature(RUAccent.load).parameters:
        return
    import ruaccent

    workdir = ruaccent_dir(root)
    workdir.mkdir(parents=True, exist_ok=True)
    pkg_koziev = Path(ruaccent.__file__).resolve().parent / "koziev"
    data_koziev = workdir / "koziev"
    done = workdir / RUACCENT_DONE
    original = RUAccent.load

    @functools.wraps(original)
    def load(self: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("workdir", str(workdir))
        omograph = kwargs.get("omograph_model_size") or OMOGRAPH_MODEL
        if not done.is_file() or not (workdir / "nn" / "nn_omograph" / omograph / "model.onnx").is_file():
            done.unlink(missing_ok=True)
            write_state("download", detail="модели ударений RUAccent (~700 МБ, с докачкой)")
            _LOGGER.info("download %s -> %s", RUACCENT_REPO, workdir)
            try:
                snapshot_download(RUACCENT_REPO, local_dir=str(workdir), allow_patterns=ruaccent_patterns(omograph))
            except Exception as err:
                raise RuntimeError(
                    f"не удалось скачать модели ударений с HuggingFace: {err}. "
                    "Загрузите архив моделей с ПК (вкладка «Озвучка»)"
                ) from err
            done.write_text(omograph, encoding="utf-8")
        if pkg_koziev.is_symlink():
            if pkg_koziev.resolve() != data_koziev.resolve():
                pkg_koziev.unlink()
        elif pkg_koziev.is_dir():
            shutil.rmtree(pkg_koziev, ignore_errors=True)
        if not pkg_koziev.is_symlink():
            pkg_koziev.symlink_to(data_koziev, target_is_directory=True)
        write_state("g2p", detail="модели ударений RUAccent")
        return original(self, *args, **kwargs)

    load._ha_voice = True  # type: ignore[attr-defined]
    RUAccent.load = load


def models_ready(root: Path | None = None) -> bool:
    """True when Kokoro and RUAccent can start without the internet."""
    root = root or data_root()
    return (
        all((root / rel).is_file() for rel in _NEED)
        and (root / "espeak-data" / "ru_dict").is_file()
        and (ruaccent_dir(root) / RUACCENT_DONE).is_file()
    )


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
        _LOGGER.info("Kokoro-ru: assets in %s", self.root)
        self._ensure_files()
        write_state("g2p", detail="ударения RUAccent")
        self._load_g2p()
        write_state("onnx", detail="модель Kokoro")
        for name in VOICES:
            self._styles[name] = np.fromfile(self.root / "voices" / f"{name}.bin", dtype=np.float32).reshape(510, 256)
        # preload default (sveta/masha share one onnx)
        self._session_for(DEFAULT_VOICE)
        write_state("warmup", detail="пробная фраза")
        self.synthesize("Проверка.", DEFAULT_VOICE)
        self.ready = True
        self.error = None
        _LOGGER.info("Kokoro-ru ready, voices=%s", ", ".join(VOICES))

    def _ensure_files(self) -> None:
        # Offline start: everything on disk means no network at all. The small
        # text files are re-checked at most once a week, and only while
        # HuggingFace answers.
        stamp = self.root / ".ha-voice-refreshed"
        try:
            fresh = time.time() - stamp.stat().st_mtime < REFRESH_EVERY_S
        except OSError:
            fresh = False
        online = not fresh and os.environ.get("HF_HUB_OFFLINE", "") not in ("1", "true")
        refreshed = False
        for rel in _NEED:
            dest = self.root / rel
            have = dest.is_file() and dest.stat().st_size > 0
            if have and not (online and rel in _REFRESH):
                continue
            write_state("download", detail=rel)
            _LOGGER.info("%s %s", "check" if have else "download", rel)
            try:
                hf_hub_download(REPO, rel, local_dir=str(self.root))
                refreshed = refreshed or have
            except Exception as err:
                if not have:
                    raise RuntimeError(
                        f"не удалось скачать {rel} с HuggingFace: {err}. "
                        "Загрузите архив моделей с ПК (вкладка «Озвучка»)"
                    ) from err
                _LOGGER.warning("HuggingFace недоступен (%s), работаю с сохранёнными файлами", err)
                online = False
        if refreshed:
            stamp.touch()
        ru_dict = self.root / "espeak-data" / "ru_dict"
        if not ru_dict.is_file():
            write_state("download", detail="espeak-data/")
            _LOGGER.info("download espeak-data/")
            snapshot_download(REPO, local_dir=str(self.root), allow_patterns=["espeak-data/**"])

    def _load_g2p(self) -> None:
        _ruaccent_workdir(self.root)
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

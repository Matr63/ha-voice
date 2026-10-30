"""Runtime settings editable from the UI and persisted to JSON."""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("ha-voice.settings")

LIMITS: dict[str, tuple[float, float]] = {
    "gate_threshold": (0.2, 0.95),
    "diary_vad_rms": (0.001, 0.2),
    "diary_silence_ms": (200, 5000),
    "diary_min_speech_ms": (100, 5000),
    "diary_max_utterance_ms": (2000, 60000),
    "diary_keep_audio": (0, 20000),
}


TTS_VOICES = ("sveta", "masha", "dima")


@dataclass(frozen=True)
class Settings:
    gate_require_match: bool = True
    gate_threshold: float = 0.55
    diary_enabled: bool = True
    diary_vad_rms: float = 0.012
    diary_silence_ms: int = 700
    diary_min_speech_ms: int = 400
    diary_max_utterance_ms: int = 15000
    diary_keep_audio: int = 500
    tts_voice: str = "sveta"


def _env_voice(name: str, default: str) -> str:
    raw = (os.environ.get(name) or "").strip().lower()
    return raw if raw in TTS_VOICES else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_num(name: str, default: float, cast: type) -> Any:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return cast(float(raw))
    except ValueError:
        return default


def _defaults_from_env() -> Settings:
    d = Settings()
    return Settings(
        gate_require_match=_env_bool("GATE_REQUIRE_MATCH", d.gate_require_match),
        gate_threshold=_env_num("GATE_THRESHOLD", d.gate_threshold, float),
        diary_enabled=_env_bool("DIALOGUE_ENABLED", d.diary_enabled),
        diary_vad_rms=_env_num("DIALOGUE_VAD_RMS", d.diary_vad_rms, float),
        diary_silence_ms=_env_num("DIALOGUE_SILENCE_MS", d.diary_silence_ms, int),
        diary_min_speech_ms=_env_num("DIALOGUE_MIN_SPEECH_MS", d.diary_min_speech_ms, int),
        diary_max_utterance_ms=_env_num("DIALOGUE_MAX_UTTERANCE_MS", d.diary_max_utterance_ms, int),
        diary_keep_audio=_env_num("DIALOGUE_KEEP_AUDIO", d.diary_keep_audio, int),
        tts_voice=_env_voice("TTS_VOICE", d.tts_voice),
    )


def _coerce(name: str, value: Any, current: Any) -> Any:
    if name == "tts_voice":
        v = str(value).strip().lower()
        return v if v in TTS_VOICES else current
    if isinstance(current, str):
        return str(value)
    if isinstance(current, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    cast = int if isinstance(current, int) else float
    num = cast(float(value))
    lo, hi = LIMITS.get(name, (float("-inf"), float("inf")))
    return cast(min(max(num, lo), hi))


class SettingsStore:
    """Values saved from the UI override add-on options / env defaults."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._value = self._load()

    def _load(self) -> Settings:
        value = _defaults_from_env()
        if self.path.is_file():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                value = self._merge(value, saved)
            except Exception:
                _LOGGER.exception("Failed to read %s, using defaults", self.path)
        return value

    @staticmethod
    def _merge(base: Settings, data: dict[str, Any]) -> Settings:
        changes: dict[str, Any] = {}
        for f in fields(Settings):
            if f.name in data:
                try:
                    changes[f.name] = _coerce(f.name, data[f.name], getattr(base, f.name))
                except (TypeError, ValueError):
                    continue
        return replace(base, **changes)

    def get(self) -> Settings:
        return self._value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self._value)

    def update(self, data: dict[str, Any]) -> Settings:
        with self._lock:
            self._value = self._merge(self._value, data)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(asdict(self._value), indent=2), encoding="utf-8")
                tmp.replace(self.path)
            except Exception:
                _LOGGER.exception("Failed to save settings to %s", self.path)
            return self._value

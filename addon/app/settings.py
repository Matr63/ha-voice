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
    "wake_threshold": (0.1, 0.95),
    "command_silence_ms": (300, 3000),
    "command_max_ms": (3000, 30000),
    "command_wait_s": (2, 30),
    "tv_duck_level": (0, 90),
    "sat_volume": (0, 100),
    "sat_mic_gain": (0, 36),
    "sat_led_brightness": (0, 100),
    "mic_boost_db": (0, 36),
    "llm_threads": (0, 64),
    "sound_volume": (10, 100),
    "llm_ctx": (2048, 32768),
}

ENUMS: dict[str, tuple[str, ...]] = {
    "wake_backend": ("local", "wyoming"),
    "assistant_backend": ("ha", "openai", "off"),
    "tv_duck": ("volume", "mute", "pause", "off"),
    "mic_channel": ("auto", "left", "right", "mix"),
}

# Never sent back to the UI in clear text.
SECRETS = ("llm_api_key", "ha_token")
SECRET_MASK = "••••••"

DEFAULT_DOMAINS = (
    "light,switch,fan,cover,climate,media_player,scene,script,input_boolean,"
    "vacuum,humidifier,sensor,binary_sensor,weather"
)


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
    # satellite (Waveshare board)
    sat_volume: int = 60
    # short sounds on the board: "listening" after the wake word, and errors
    sound_wake: bool = True
    sound_error: bool = True
    sound_volume: int = 60
    # TV next to the board: diary for known voices only, stricter commands while it plays
    diary_known_only: bool = False
    tv_entity: str = ""             # media_player.* in Home Assistant
    # while a board listens and answers: quieter TV ("volume"), "mute", "pause" or "off"
    tv_duck: str = "volume"
    tv_duck_level: int = 25         # % of the TV's volume while ducked
    sat_mic_gain: int = 36          # ES7210 hardware gain, dB (3 dB steps)
    sat_led_brightness: int = 80
    mic_boost_db: int = 6           # digital gain on the PC (the board's loud slot is picked first)
    mic_channel: str = "auto"       # which ES7210 slot to use
    # sleep mode: by schedule and by voice
    sleep_schedule: bool = False
    sleep_start: str = "23:00"
    sleep_end: str = "07:00"
    sleep_phrases: str = "спи, спать, режим сна, спокойной ночи"
    awake_phrases: str = "не спи, проснись, доброе утро"
    # wake word on the PC
    wake_backend: str = "local"
    wake_word: str = "alexa"
    wake_threshold: float = 0.5
    wake_uri: str = "tcp://127.0.0.1:10400"
    # any Russian phrase, matched in the recognised text (no model training)
    wake_phrases: str = "алекса, колонка"
    command_silence_ms: int = 800
    command_max_ms: int = 10000
    # after the wake word: how long to wait for the command to start
    command_wait_s: int = 6
    # assistant
    assistant_backend: str = "ha"
    ha_agent_id: str = ""
    llm_url: str = "http://127.0.0.1:11434/v1"
    llm_model: str = "qwen2.5:7b"
    llm_api_key: str = ""
    llm_prompt: str = ""
    # with the HA agent: phrases HA did not understand go to the LLM above
    llm_fallback: bool = True
    # room per board, JSON {"<board name>": "<HA area>"}; a board not listed
    # takes the area of its ESPHome device in HA
    sat_rooms: str = "{}"
    # sound per board, JSON {"<board>": {"speaker": false, "out": "<board>"}}:
    # a board without a speaker answers through another one
    sat_audio: str = "{}"
    # local model run by the add-on (llama.cpp); empty = none
    llm_local_model: str = ""
    llm_threads: int = 0            # 0 = all cores but one
    llm_ctx: int = 8192
    assistant_domains: str = DEFAULT_DOMAINS
    # Home Assistant access (empty = Supervisor token on HAOS)
    ha_url: str = ""
    ha_token: str = ""


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return raw.strip() if raw and raw.strip() else default


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
        wake_backend=_env_str("WAKE_BACKEND", d.wake_backend),
        wake_word=_env_str("WAKE_WORD", d.wake_word),
        wake_threshold=_env_num("WAKE_THRESHOLD", d.wake_threshold, float),
        wake_uri=_env_str("WAKE_URI", d.wake_uri),
        assistant_backend=_env_str("ASSISTANT_BACKEND", d.assistant_backend),
        ha_agent_id=_env_str("HA_AGENT_ID", d.ha_agent_id),
        llm_url=_env_str("LLM_URL", d.llm_url),
        llm_model=_env_str("LLM_MODEL", d.llm_model),
        llm_api_key=_env_str("LLM_API_KEY", d.llm_api_key),
        llm_fallback=_env_bool("LLM_FALLBACK", d.llm_fallback),
        ha_url=_env_str("HA_URL", d.ha_url),
        ha_token=_env_str("HA_TOKEN", d.ha_token),
    )


def _coerce(name: str, value: Any, current: Any) -> Any:
    if name == "tts_voice":
        v = str(value).strip().lower()
        return v if v in TTS_VOICES else current
    if name in ENUMS:
        v = str(value).strip().lower()
        return v if v in ENUMS[name] else current
    if name in SECRETS and str(value) == SECRET_MASK:
        return current
    if name in ("sleep_start", "sleep_end"):
        v = str(value).strip()
        try:
            h, m = (int(x) for x in v.split(":"))
        except ValueError:
            return current
        return f"{h:02d}:{m:02d}" if 0 <= h < 24 and 0 <= m < 60 else current
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

    def to_public_dict(self) -> dict[str, Any]:
        data = asdict(self._value)
        for key in SECRETS:
            if data.get(key):
                data[key] = SECRET_MASK
        return data

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

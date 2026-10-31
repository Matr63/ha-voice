"""PCM helpers. Internally all audio is 16 kHz mono signed 16-bit."""
from __future__ import annotations

import io
import wave

import numpy as np

TARGET_RATE = 16000


def pcm16_to_float(pcm: bytes) -> np.ndarray:
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def float_to_pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def resample(audio: np.ndarray, src_rate: int, dst_rate: int = TARGET_RATE) -> np.ndarray:
    if src_rate == dst_rate or audio.size == 0:
        return audio
    new_len = max(1, int(round(audio.size * dst_rate / float(src_rate))))
    x_old = np.arange(audio.size, dtype=np.float64) / src_rate
    x_new = np.arange(new_len, dtype=np.float64) / dst_rate
    return np.interp(x_new, x_old, audio).astype(np.float32)


def normalize_pcm(pcm: bytes, rate: int, channels: int = 1) -> bytes:
    audio = pcm16_to_float(pcm)
    if channels > 1:
        usable = audio.size - audio.size % channels
        audio = audio[:usable].reshape(-1, channels).mean(axis=1)
    return float_to_pcm16(resample(audio, rate))


def decode_body(body: bytes, rate: int = TARGET_RATE) -> bytes:
    """WAV (16-bit PCM) or raw PCM16 mono at `rate` → 16 kHz mono PCM16."""
    if body[:4] == b"RIFF":
        try:
            with wave.open(io.BytesIO(body), "rb") as w:
                if w.getsampwidth() != 2:
                    raise ValueError("Нужен WAV 16-bit PCM")
                frames = w.readframes(w.getnframes())
                return normalize_pcm(frames, w.getframerate(), w.getnchannels())
        except wave.Error as err:
            raise ValueError(f"Не удалось прочитать WAV: {err}") from err
    return normalize_pcm(body, rate)


def to_wav(pcm: bytes, rate: int = TARGET_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def duration_ms(pcm: bytes, rate: int = TARGET_RATE) -> int:
    return int(len(pcm) / 2 / rate * 1000)


def rms(pcm: bytes) -> float:
    audio = pcm16_to_float(pcm)
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(audio * audio)))


def split_stereo(pcm: bytes) -> tuple[np.ndarray, np.ndarray]:
    audio = pcm16_to_float(pcm)
    audio = audio[: audio.size - audio.size % 2].reshape(-1, 2)
    return audio[:, 0], audio[:, 1]


def apply_gain(audio: np.ndarray, gain_db: float) -> bytes:
    """Digital gain with a soft knee above -3 dBFS instead of hard clipping."""
    if gain_db:
        audio = audio * (10.0 ** (gain_db / 20.0))
        mag = np.abs(audio)
        knee = 0.7
        over = mag > knee
        if over.any():
            audio = np.where(over, np.sign(audio) * (knee + (1 - knee) * np.tanh((mag - knee) / (1 - knee))), audio)
    return float_to_pcm16(audio)

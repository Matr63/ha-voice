"""Satellite firmware: keep factory.bin / ota.bin and push OTA to a board.

factory.bin - first flash over USB (web.esphome.io or ESP Web Tools)
ota.bin     - Wi-Fi update: POSTed to http://<board>/update (ESPHome web_server OTA)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import aiohttp

_LOGGER = logging.getLogger("ha-voice.firmware")

KINDS = {
    "factory": "ha-voice-satellite.factory.bin",
    "ota": "ha-voice-satellite.ota.bin",
}
DEFAULT_RELEASE = "https://github.com/Matr63/ha-voice/releases/download/firmware-latest"
ESP_IMAGE_MAGIC = 0xE9
MIN_SIZE = 64 * 1024
MAX_SIZE = 16 * 1024 * 1024


class FirmwareStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.release_url = os.environ.get("FIRMWARE_URL", DEFAULT_RELEASE).rstrip("/")
        self.ota_jobs: dict[str, dict[str, Any]] = {}

    def path(self, kind: str) -> Path:
        if kind not in KINDS:
            raise KeyError(kind)
        return self.root / KINDS[kind]

    def _meta_path(self) -> Path:
        return self.root / "meta.json"

    def _meta(self) -> dict[str, Any]:
        try:
            return json.loads(self._meta_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _set_meta(self, kind: str, source: str) -> None:
        meta = self._meta()
        meta[kind] = {"source": source, "ts": time.time()}
        self._meta_path().write_text(json.dumps(meta), encoding="utf-8")

    def info(self) -> dict[str, Any]:
        meta = self._meta()
        files = {}
        for kind in KINDS:
            p = self.path(kind)
            if p.is_file():
                data = p.read_bytes()
                files[kind] = {
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()[:16],
                    "source": (meta.get(kind) or {}).get("source", "upload"),
                    "ts": (meta.get(kind) or {}).get("ts", p.stat().st_mtime),
                }
            else:
                files[kind] = None
        return {"files": files, "release_url": self.release_url, "ota": self.ota_jobs}

    @staticmethod
    def validate(data: bytes) -> None:
        if len(data) < MIN_SIZE or len(data) > MAX_SIZE:
            raise ValueError(f"Странный размер прошивки: {len(data)} байт")
        if data[0] != ESP_IMAGE_MAGIC:
            raise ValueError("Это не образ ESP32 (нет сигнатуры 0xE9)")

    def save(self, kind: str, data: bytes, source: str = "upload") -> dict[str, Any]:
        self.validate(data)
        tmp = self.path(kind).with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(self.path(kind))
        self._set_meta(kind, source)
        return self.info()

    async def fetch_latest(self) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for kind, name in KINDS.items():
                url = f"{self.release_url}/{name}"
                async with session.get(url) as resp:
                    if resp.status != 200:
                        raise ValueError(f"Не удалось скачать {name}: HTTP {resp.status}")
                    data = await resp.read()
                self.save(kind, data, source="github")
                _LOGGER.info("Firmware %s downloaded (%d bytes)", name, len(data))
        return self.info()

    async def ota(self, ip: str) -> dict[str, Any]:
        """Uploads ota.bin to the board's web_server; it reboots when done."""
        p = self.path("ota")
        if not p.is_file():
            raise ValueError("Нет ota.bin — скачайте или загрузите прошивку")
        job = self.ota_jobs[ip] = {"ip": ip, "status": "uploading", "started": time.time(), "error": None}
        form = aiohttp.FormData()
        form.add_field("update", p.read_bytes(), filename="firmware.ota.bin", content_type="application/octet-stream")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
                async with session.post(f"http://{ip}/update", data=form) as resp:
                    text = await resp.text()
                    if resp.status >= 400 or "fail" in text.lower():
                        raise ValueError(f"Плата ответила {resp.status}: {text[:200]}")
            job["status"] = "done"
            _LOGGER.info("OTA to %s done", ip)
        except Exception as err:
            job["status"] = "error"
            job["error"] = str(err) or type(err).__name__
            _LOGGER.warning("OTA to %s failed: %s", ip, job["error"])
        job["finished"] = time.time()
        return job

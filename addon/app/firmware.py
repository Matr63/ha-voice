"""Satellite firmware: keep factory.bin / ota.bin per board and flash them.

factory.bin - first flash over USB: from the browser (ESP Web Tools /
              web.esphome.io) or from a USB port of the HA machine (esptool)
ota.bin     - Wi-Fi update: POSTed to http://<board>/update (ESPHome web_server OTA)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

_LOGGER = logging.getLogger("ha-voice.firmware")

KINDS = ("factory", "ota")
BOARDS: dict[str, dict[str, str]] = {
    "waveshare": {"label": "Waveshare ESP32-S3-AUDIO", "prefix": "ha-voice-satellite", "chip": "esp32s3"},
    "xvf3800": {"label": "ReSpeaker XVF3800 + XIAO ESP32-S3", "prefix": "ha-voice-xvf3800", "chip": "esp32s3"},
}
DEFAULT_BOARD = "waveshare"
DEFAULT_RELEASE = "https://github.com/Matr63/ha-voice/releases/download/firmware-latest"
ESP_IMAGE_MAGIC = 0xE9
MIN_SIZE = 64 * 1024
MAX_SIZE = 16 * 1024 * 1024
_PROGRESS_RE = re.compile(r"\((\d+)\s*%\)")


def board_key(board: str | None) -> str:
    board = (board or DEFAULT_BOARD).strip().lower()
    if board not in BOARDS:
        raise ValueError(f"Неизвестная плата: {board}")
    return board


def file_name(board: str, kind: str) -> str:
    if kind not in KINDS:
        raise KeyError(kind)
    return f"{BOARDS[board_key(board)]['prefix']}.{kind}.bin"


class FirmwareStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.release_url = os.environ.get("FIRMWARE_URL", DEFAULT_RELEASE).rstrip("/")
        self.ota_jobs: dict[str, dict[str, Any]] = {}
        self.serial_job: dict[str, Any] | None = None

    def path(self, kind: str, board: str | None = None) -> Path:
        return self.root / file_name(board_key(board), kind)

    def _meta_path(self) -> Path:
        return self.root / "meta.json"

    def _meta(self) -> dict[str, Any]:
        try:
            return json.loads(self._meta_path().read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _set_meta(self, name: str, source: str) -> None:
        meta = self._meta()
        meta[name] = {"source": source, "ts": time.time()}
        self._meta_path().write_text(json.dumps(meta), encoding="utf-8")

    def _files(self, board: str, meta: dict[str, Any]) -> dict[str, Any]:
        files: dict[str, Any] = {}
        for kind in KINDS:
            p = self.path(kind, board)
            # meta.json of 2.1 was keyed by kind only (Waveshare was the only board)
            m = meta.get(p.name) or (meta.get(kind) if board == DEFAULT_BOARD else None) or {}
            if p.is_file():
                data = p.read_bytes()
                files[kind] = {
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()[:16],
                    "source": m.get("source", "upload"),
                    "ts": m.get("ts", p.stat().st_mtime),
                }
            else:
                files[kind] = None
        return files

    def info(self) -> dict[str, Any]:
        meta = self._meta()
        boards = {b: {"label": v["label"], "files": self._files(b, meta)} for b, v in BOARDS.items()}
        return {
            "boards": boards,
            "files": boards[DEFAULT_BOARD]["files"],
            "release_url": self.release_url,
            "ota": self.ota_jobs,
            "serial": self.serial_job,
        }

    @staticmethod
    def validate(data: bytes) -> None:
        if len(data) < MIN_SIZE or len(data) > MAX_SIZE:
            raise ValueError(f"Странный размер прошивки: {len(data)} байт")
        if data[0] != ESP_IMAGE_MAGIC:
            raise ValueError("Это не образ ESP32 (нет сигнатуры 0xE9)")

    def save(self, kind: str, data: bytes, source: str = "upload", board: str | None = None) -> dict[str, Any]:
        self.validate(data)
        dest = self.path(kind, board)
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        self._set_meta(dest.name, source)
        return self.info()

    async def fetch_latest(self, board: str | None = None) -> dict[str, Any]:
        boards = [board_key(board)] if board else list(BOARDS)
        errors: list[str] = []
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for b in boards:
                for kind in KINDS:
                    name = file_name(b, kind)
                    url = f"{self.release_url}/{name}"
                    try:
                        async with session.get(url) as resp:
                            if resp.status != 200:
                                raise ValueError(f"HTTP {resp.status}")
                            data = await resp.read()
                        self.save(kind, data, source="github", board=b)
                        _LOGGER.info("Firmware %s downloaded (%d bytes)", name, len(data))
                    except Exception as err:
                        errors.append(f"{name}: {err}")
        if errors and len(errors) == len(boards) * len(KINDS):
            raise ValueError("Не удалось скачать прошивку: " + "; ".join(errors))
        return {**self.info(), "errors": errors}

    async def ota(self, ip: str, board: str | None = None) -> dict[str, Any]:
        """Uploads ota.bin to the board's web_server; it reboots when done."""
        p = self.path("ota", board)
        if not p.is_file():
            raise ValueError(f"Нет {p.name} — скачайте или загрузите прошивку")
        job = self.ota_jobs[ip] = {"ip": ip, "board": board_key(board), "status": "uploading", "started": time.time(), "error": None}
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

    # ------------------------------------------------- USB of the HA machine
    @staticmethod
    def serial_ports() -> list[dict[str, str]]:
        """USB serial devices the add-on can see (config.yaml: uart: true)."""
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        by_id = Path("/dev/serial/by-id")
        if by_id.is_dir():
            for link in sorted(by_id.iterdir()):
                real = str(link.resolve())
                seen.add(real)
                name = link.name.replace("usb-", "").rsplit("-if", 1)[0].replace("_", " ")
                hint = ""
                if "Espressif" in link.name or "303a" in link.name.lower():
                    hint = "ESP32-S3 (встроенный USB)"
                out.append({"port": str(link), "device": real, "name": name, "hint": hint})
        for pattern in ("ttyACM*", "ttyUSB*"):
            for dev in sorted(Path("/dev").glob(pattern)):
                if str(dev) not in seen:
                    out.append({"port": str(dev), "device": str(dev), "name": dev.name, "hint": ""})
        return out

    def start_serial_flash(self, port: str, board: str | None) -> dict[str, Any]:
        if self.serial_job and self.serial_job["status"] == "running":
            raise ValueError("Прошивка по USB уже идёт")
        if port not in {p["port"] for p in self.serial_ports()}:
            raise ValueError("Порт не найден — подключите плату к USB сервера HA и обновите список")
        image = self.path("factory", board)
        if not image.is_file():
            raise ValueError(f"Нет {image.name} — нажмите «Скачать последнюю»")
        self.serial_job = {"port": port, "board": board_key(board), "status": "running", "progress": 0,
                           "log": [], "error": None, "started": time.time()}
        return self.serial_job

    async def serial_flash(self, job: dict[str, Any], erase: bool) -> None:
        chip = BOARDS[job["board"]]["chip"]
        image = self.path("factory", job["board"])
        args = [sys.executable, "-m", "esptool", "--chip", chip, "--port", job["port"], "--baud", "460800",
                "write_flash", "-z"]
        if erase:
            args.append("--erase-all")
        args += ["0x0", str(image)]
        log: list[str] = job["log"]
        try:
            proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            assert proc.stdout is not None
            buf = b""
            while True:
                chunk = await proc.stdout.read(256)
                if not chunk:
                    break
                buf += chunk
                # esptool redraws progress with \r
                *lines, buf = re.split(rb"[\r\n]", buf)
                for raw in lines:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    m = _PROGRESS_RE.search(line)
                    if m:
                        job["progress"] = int(m.group(1))
                        continue
                    log.append(line)
                    del log[:-40]
            code = await proc.wait()
            if code != 0:
                raise ValueError(f"esptool завершился с кодом {code}: {log[-1] if log else ''}")
            job["status"] = "done"
            job["progress"] = 100
            _LOGGER.info("USB flash %s on %s done", image.name, job["port"])
        except Exception as err:
            job["status"] = "error"
            job["error"] = str(err) or type(err).__name__
            _LOGGER.warning("USB flash on %s failed: %s", job["port"], job["error"])
        job["finished"] = time.time()

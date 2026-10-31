"""Local LLM on the HA machine: GGUF models + llama.cpp's llama-server.

Models live in LLM_DIR (/share/ha-voice/llm on HAOS: outside the add-on
backup, and reachable over Samba). They get there three ways: a preset or any
HuggingFace URL downloaded here, an upload from the panel, or a copy into the
folder. llama-server speaks the OpenAI API on 127.0.0.1:LLM_PORT, so the
assistant's "openai" backend talks to it like to Ollama.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, AsyncIterator

import aiohttp

from settings import SettingsStore

_LOGGER = logging.getLogger("ha-voice.llm")

LLM_PORT = int(os.environ.get("LLM_PORT", "10360"))
LLM_URL = f"http://127.0.0.1:{LLM_PORT}/v1"
SERVER_BIN = os.environ.get("LLAMA_SERVER", "/usr/local/bin/llama-server")
_NAME_RE = re.compile(r"^[\w.\-]+\.gguf$")
_CHUNK = 1024 * 1024

# Q4_K_M quants; sizes checked on huggingface.co. All of them call tools.
PRESETS: list[dict[str, Any]] = [
    {
        "id": "qwen2.5-1.5b",
        "label": "Qwen2.5 1.5B Instruct",
        "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "file": "qwen2.5-1.5b-instruct-q4_k_m.gguf",
        "size": 1117320736,
        "note": "самая быстрая, слабый русский — для Raspberry Pi / N100",
    },
    {
        "id": "qwen2.5-3b",
        "label": "Qwen2.5 3B Instruct",
        "repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "file": "qwen2.5-3b-instruct-q4_k_m.gguf",
        "size": 2104932768,
        "note": "баланс скорости и качества, ~3 ГБ ОЗУ",
    },
    {
        "id": "qwen3-4b-2507",
        "label": "Qwen3 4B Instruct 2507",
        "repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF",
        "file": "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
        "size": 2497281120,
        "note": "лучшая из маленьких для команд дому, без «размышлений», ~4 ГБ ОЗУ",
    },
    {
        "id": "t-lite-1.0",
        "label": "T-lite-it 1.0 (7B, русская)",
        "repo": "mradermacher/T-lite-it-1.0-GGUF",
        "file": "T-lite-it-1.0.Q4_K_M.gguf",
        "size": 4681085024,
        "note": "дообучена на русском (Т-Банк), нужен быстрый ПК, ~6 ГБ ОЗУ",
    },
    {
        "id": "qwen2.5-7b",
        "label": "Qwen2.5 7B Instruct",
        "repo": "bartowski/Qwen2.5-7B-Instruct-GGUF",
        "file": "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        "size": 4683074240,
        "note": "умнее, но медленно на CPU, ~6 ГБ ОЗУ",
    },
]


def hf_url(repo: str, file: str) -> str:
    base = (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
    return f"{base}/{repo}/resolve/main/{file}?download=true"


def safe_name(name: str) -> str:
    name = Path(str(name or "")).name.strip()
    if not _NAME_RE.match(name):
        raise ValueError("Нужен файл модели .gguf (латиница, цифры, точка, дефис)")
    return name


class LlmManager:
    def __init__(self, settings: SettingsStore, root: Path | None = None) -> None:
        self.settings = settings
        self.root = Path(root or os.environ.get("LLM_DIR", "/share/ha-voice/llm"))
        self.log_path = Path(os.environ.get("LLM_LOG", "/tmp/ha-voice-logs/llm.log"))
        self.job: dict[str, Any] | None = None
        self._job_task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._watch: asyncio.Task | None = None
        self.model: str | None = None
        self.state = "stopped"  # stopped | loading | ready | error
        self.error: str | None = None
        self.started_at: float | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            _LOGGER.warning("LLM folder %s: %s", self.root, err)

    # ------------------------------------------------------------- models
    @property
    def available(self) -> bool:
        return Path(SERVER_BIN).is_file()

    def models(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.glob("*.gguf")):
            st = p.stat()
            out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
        return out

    def path(self, name: str) -> Path:
        return self.root / safe_name(name)

    def delete(self, name: str) -> None:
        p = self.path(name)
        if self.model == p.name:
            raise ValueError("Сначала остановите эту модель")
        if not p.is_file():
            raise KeyError(name)
        p.unlink()

    def status(self) -> dict[str, Any]:
        s = self.settings.get()
        disk = None
        try:
            disk = shutil.disk_usage(self.root).free
        except OSError:
            pass
        return {
            "available": self.available,
            "state": self.state,
            "model": self.model,
            "error": self.error,
            "started_at": self.started_at,
            "url": LLM_URL,
            "active": s.llm_url.rstrip("/") == LLM_URL
            and (s.assistant_backend == "openai" or (s.assistant_backend == "ha" and s.llm_fallback)),
            "fallback": s.assistant_backend == "ha" and s.llm_fallback,
            "dir": str(self.root),
            "disk_free": disk,
            "job": self.job,
        }

    # ---------------------------------------------------------- downloads
    def _start_job(self, name: str, source: str, total: int | None) -> dict[str, Any]:
        if self.job and self.job["status"] == "running":
            raise ValueError("Уже идёт загрузка другой модели")
        self.job = {"name": name, "source": source, "status": "running", "done": 0, "total": total,
                    "error": None, "started": time.time(), "speed": 0}
        return self.job

    def download(self, preset: str | None = None, url: str | None = None) -> dict[str, Any]:
        if preset:
            p = next((x for x in PRESETS if x["id"] == preset), None)
            if p is None:
                raise ValueError("Неизвестная модель")
            name, src, total = p["file"], hf_url(p["repo"], p["file"]), p["size"]
        else:
            src = (url or "").strip()
            if not src.startswith(("http://", "https://")):
                raise ValueError("Ссылка должна начинаться с https://")
            # huggingface.co/<repo>/blob/main/x.gguf -> resolve/main
            src = src.replace("/blob/", "/resolve/")
            name, total = safe_name(src.split("?", 1)[0].rsplit("/", 1)[-1]), None
        if self.path(name).is_file():
            raise ValueError(f"{name} уже скачана")
        job = self._start_job(name, src, total)
        self._job_task = asyncio.create_task(self._download(job))
        return job

    def cancel(self) -> None:
        if self._job_task and not self._job_task.done():
            self._job_task.cancel()

    async def _download(self, job: dict[str, Any]) -> None:
        dest = self.path(job["name"])
        part = dest.with_name(dest.name + ".part")
        try:
            # resume a broken download instead of starting the 3 GB over
            have = part.stat().st_size if part.is_file() else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(job["source"], headers=headers, allow_redirects=True) as resp:
                    if resp.status == 416:
                        have = 0
                        part.unlink(missing_ok=True)
                        raise ValueError("Файл на сервере изменился, запустите загрузку ещё раз")
                    if resp.status not in (200, 206):
                        raise ValueError(f"HTTP {resp.status} — проверьте ссылку")
                    if resp.status == 200:
                        have = 0
                    length = resp.content_length
                    if length is not None:
                        job["total"] = have + length
                    job["done"] = have
                    await self._write(resp.content.iter_chunked(_CHUNK), part, job, append=have > 0)
            self._finish(part, dest, job)
        except asyncio.CancelledError:
            job["status"] = "cancelled"
            raise
        except Exception as err:
            job["status"] = "error"
            job["error"] = str(err) or err.__class__.__name__
            _LOGGER.warning("LLM download %s failed: %s", job["name"], job["error"])

    async def upload(self, name: str, stream: AsyncIterator[bytes], total: int | None) -> dict[str, Any]:
        name = safe_name(name)
        dest = self.path(name)
        if dest.is_file():
            raise ValueError(f"{name} уже есть")
        job = self._start_job(name, "upload", total)
        part = dest.with_name(dest.name + ".part")
        try:
            await self._write(stream, part, job, append=False)
            self._finish(part, dest, job)
        except Exception as err:
            job["status"] = "error"
            job["error"] = str(err) or err.__class__.__name__
            part.unlink(missing_ok=True)
            raise
        return job

    async def _write(self, chunks: AsyncIterator[bytes], part: Path, job: dict[str, Any], append: bool) -> None:
        t0, d0 = time.time(), job["done"]
        with open(part, "ab" if append else "wb") as fh:
            async for chunk in chunks:
                await asyncio.to_thread(fh.write, chunk)
                job["done"] += len(chunk)
                dt = time.time() - t0
                if dt > 1:
                    job["speed"] = int((job["done"] - d0) / dt)

    def _finish(self, part: Path, dest: Path, job: dict[str, Any]) -> None:
        with open(part, "rb") as fh:
            magic = fh.read(4)
        if magic != b"GGUF":
            part.unlink(missing_ok=True)
            raise ValueError("Это не файл GGUF (нужен формат llama.cpp)")
        part.replace(dest)
        job["status"] = "done"
        job["done"] = dest.stat().st_size
        job["total"] = job["done"]
        _LOGGER.info("LLM model %s ready (%d bytes)", dest.name, job["done"])

    # ------------------------------------------------------------- server
    async def restore(self) -> None:
        """Starts the model chosen last time (the add-on restarted)."""
        name = self.settings.get().llm_local_model
        if name and self.available:
            try:
                await self.start(name, activate=False)
            except Exception as err:
                _LOGGER.warning("LLM %s not started: %s", name, err)

    async def start(self, name: str, activate: bool = True) -> dict[str, Any]:
        if not self.available:
            raise ValueError("llama-server не собран в этом образе — обновите дополнение")
        path = self.path(name)
        if not path.is_file():
            raise ValueError(f"Нет файла {path.name}")
        await self.stop(forget=False)
        s = self.settings.get()
        threads = s.llm_threads or max(1, (os.cpu_count() or 2) - 1)
        args = [
            SERVER_BIN, "-m", str(path), "--host", "127.0.0.1", "--port", str(LLM_PORT),
            "--alias", path.stem, "-c", str(s.llm_ctx), "-t", str(threads),
            # chat template from the GGUF: tool calling needs it
            "--jinja",
        ]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "wb")
        try:
            self._proc = await asyncio.create_subprocess_exec(*args, stdout=log, stderr=asyncio.subprocess.STDOUT)
        finally:
            log.close()
        self.model, self.state, self.error, self.started_at = path.name, "loading", None, time.time()
        _LOGGER.info("llama-server pid=%s model=%s threads=%s ctx=%s", self._proc.pid, path.name, threads, s.llm_ctx)
        changes: dict[str, Any] = {"llm_local_model": path.name}
        if activate:
            changes.update(llm_url=LLM_URL, llm_model=path.stem, llm_api_key="")
            # HA agent + fallback stays: exact commands in HA, the rest to this model
            if not (s.assistant_backend == "ha" and s.llm_fallback):
                changes["assistant_backend"] = "openai"
        self.settings.update(changes)
        self._watch = asyncio.create_task(self._watch_proc(self._proc))
        return self.status()

    async def stop(self, forget: bool = True) -> dict[str, Any]:
        proc, self._proc = self._proc, None
        if self._watch:
            self._watch.cancel()
            self._watch = None
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        self.state, self.model, self.error, self.started_at = "stopped", None, None, None
        if forget:
            self.settings.update({"llm_local_model": ""})
        return self.status()

    async def _watch_proc(self, proc: asyncio.subprocess.Process) -> None:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                while proc.returncode is None and self.state == "loading":
                    try:
                        async with session.get(f"http://127.0.0.1:{LLM_PORT}/health") as resp:
                            if resp.status == 200:
                                self.state = "ready"
                                _LOGGER.info("llama-server ready: %s", self.model)
                                break
                    except (aiohttp.ClientError, asyncio.TimeoutError):
                        pass
                    await asyncio.sleep(1)
            code = await proc.wait()
            if self._proc is proc:
                self.state = "error"
                self.error = f"llama-server завершился (код {code}): " + " | ".join(self.log_tail(4))
                _LOGGER.warning("%s", self.error)
        except asyncio.CancelledError:
            pass

    def log_tail(self, lines: int = 40) -> list[str]:
        try:
            text = self.log_path.read_bytes()[-16384:].decode("utf-8", "replace")
        except OSError:
            return []
        return [ln for ln in text.splitlines() if ln.strip()][-lines:]

    async def close(self) -> None:
        await self.stop(forget=False)

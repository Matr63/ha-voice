"""Kokoro + RUAccent models as one .zip: take them out of the add-on (backup,
another server) or bring them in from a PC when HuggingFace is unreachable.

Layout inside the archive (the PC script makes the same):
    kokoro-ru/...   -> /data/models/kokoro-ru
    ruaccent/...    -> /data/models/ruaccent
A single top folder around them (e.g. "ha-voice-models/") is fine.
"""
from __future__ import annotations

import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator

from tts_engine import OMOGRAPH_MODEL, RUACCENT_DONE, data_root, models_ready, ruaccent_dir

_LOGGER = logging.getLogger("ha-voice.models")

PARTS = ("kokoro-ru", "ruaccent")


def _dirs() -> dict[str, Path]:
    root = data_root()
    return {"kokoro-ru": root, "ruaccent": ruaccent_dir(root)}


def _files(base: Path):
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        # .cache = huggingface_hub download metadata, not needed offline
        if p.is_file() and ".cache" not in rel.parts and not p.name.endswith((".incomplete", ".lock")):
            yield p, rel


def status() -> dict[str, Any]:
    size = 0
    for base in _dirs().values():
        if base.is_dir():
            size += sum(p.stat().st_size for p, _ in _files(base))
    return {"ready": models_ready(), "size": size}


def export(dest: Path) -> Path:
    if not models_ready():
        raise ValueError("Модели ещё не скачаны полностью — экспортировать нечего")
    tmp = dest.with_suffix(".tmp")
    # ONNX and gzip dictionaries do not compress further: store, it is much faster
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for part, base in _dirs().items():
            for p, rel in _files(base):
                zf.write(p, f"{part}/{rel.as_posix()}")
    tmp.replace(dest)
    return dest


async def receive(chunks: AsyncIterator[bytes], dest: Path) -> int:
    n = 0
    with open(dest, "wb") as fh:
        async for chunk in chunks:
            fh.write(chunk)
            n += len(chunk)
    return n


def install(archive: Path) -> dict[str, Any]:
    """Unpacks a model archive into /data/models. Only kokoro-ru/ and ruaccent/
    paths are taken; anything else (or a path escaping them) is skipped."""
    dirs = _dirs()
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as err:
        raise ValueError("Это не .zip архив") from err
    with zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        prefix = ""
        if names and not any(n.split("/", 1)[0] in PARTS for n in names):
            # one wrapping folder, e.g. "ha-voice-models/kokoro-ru/..."
            prefix = names[0].split("/", 1)[0] + "/"
        found = {p: 0 for p in PARTS}
        for name in names:
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            part, _, inner = rel.partition("/")
            if part not in PARTS or not inner:
                continue
            target = (dirs[part] / inner).resolve()
            if dirs[part].resolve() not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            found[part] += 1
    if not any(found.values()):
        raise ValueError("В архиве нет папок kokoro-ru/ или ruaccent/")
    rua = dirs["ruaccent"]
    if (rua / "nn" / "nn_omograph" / OMOGRAPH_MODEL / "model.onnx").is_file() and (rua / "dictionary").is_dir():
        (rua / RUACCENT_DONE).write_text(OMOGRAPH_MODEL, encoding="utf-8")
    # a fresh set from the PC counts as just checked against HuggingFace
    if found["kokoro-ru"]:
        (dirs["kokoro-ru"] / ".ha-voice-refreshed").touch()
    _LOGGER.info("Models installed from archive: %s", found)
    return {"files": found, **status()}


def cleanup(*paths: Path) -> None:
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass

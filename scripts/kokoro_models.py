#!/usr/bin/env python3
"""Download the Kokoro-ru and RUAccent models on a PC and pack them into one
.zip for the HA Voice panel (Озвучка -> «Модели Kokoro офлайн» -> «Загрузить
архив с ПК»). For a Home Assistant server that cannot reach HuggingFace.

    pip install huggingface_hub
    python kokoro_models.py                       # -> ha-voice-kokoro-models.zip
    python kokoro_models.py --endpoint https://hf-mirror.com

Re-running continues an interrupted download (files are kept in --work).
The file lists match addon/app/tts_engine.py.
"""
from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path

KOKORO_REPO = "zaakirio/kokoro-ru"
KOKORO_FILES = [
    "ru_g2p.py",
    "kokoro-config.json",
    "config.json",
    "onnx/model_quantized.onnx",
    "onnx/model_dima_quantized.onnx",
    "voices/sveta.bin",
    "voices/masha.bin",
    "voices/dima.bin",
    "espeak-data/*",
]
RUACCENT_REPO = "ruaccent/accentuator"
OMOGRAPH_MODEL = "turbo3.1"
RUACCENT_FILES = [
    "dictionary/*",
    "nn/nn_accent/*",
    "nn/nn_stress_usage_predictor/*",
    "nn/nn_yo_homograph_resolver/*",
    f"nn/nn_omograph/{OMOGRAPH_MODEL}/*",
    "koziev/*",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="ha-voice-kokoro-models.zip", help="archive to write")
    ap.add_argument("--work", default="ha-voice-models", help="download folder (kept for resume)")
    ap.add_argument("--endpoint", help="HuggingFace mirror, e.g. https://hf-mirror.com")
    args = ap.parse_args()

    if args.endpoint:
        # must be set before huggingface_hub is imported
        os.environ["HF_ENDPOINT"] = args.endpoint.rstrip("/")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("Нужен huggingface_hub:  pip install huggingface_hub", file=sys.stderr)
        return 1

    work = Path(args.work)
    for repo, patterns, sub in (
        (KOKORO_REPO, KOKORO_FILES, "kokoro-ru"),
        (RUACCENT_REPO, RUACCENT_FILES, "ruaccent"),
    ):
        print(f"[{sub}] {repo} ...", flush=True)
        snapshot_download(repo, local_dir=str(work / sub), allow_patterns=patterns)

    missing = [p for p in ("kokoro-ru/onnx/model_quantized.onnx", f"ruaccent/nn/nn_omograph/{OMOGRAPH_MODEL}/model.onnx")
               if not (work / p).is_file()]
    if missing:
        print("Не скачались: " + ", ".join(missing), file=sys.stderr)
        return 1

    out = Path(args.out)
    n = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for sub in ("kokoro-ru", "ruaccent"):
            base = work / sub
            for p in sorted(base.rglob("*")):
                rel = p.relative_to(base)
                if p.is_file() and ".cache" not in rel.parts:
                    zf.write(p, f"{sub}/{rel.as_posix()}")
                    n += 1
    print(f"Готово: {out} ({out.stat().st_size / 1048576:.0f} МБ, {n} файлов).")
    print("Панель HA Voice -> Озвучка -> «Загрузить архив с ПК (.zip)».")
    return 0


if __name__ == "__main__":
    sys.exit(main())

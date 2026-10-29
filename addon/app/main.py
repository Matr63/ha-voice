#!/usr/bin/env python3
"""Combined speaker gate (Wyoming) + dialogue PCM server + HTTP API."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from api import start_api
from dialogue import build_runtime_from_env, run_pcm_server
from gate_server import run_gate
from speakers import SpeakerStore

_LOGGER = logging.getLogger("ha-voice")


async def amain() -> None:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    speakers_dir = Path(os.environ.get("SPEAKERS_DIR", "/data/speakers"))
    threshold = float(os.environ.get("GATE_THRESHOLD", "0.55"))
    store = SpeakerStore(speakers_dir, threshold=threshold)
    runtime = build_runtime_from_env(store)

    gate_uri = os.environ.get("GATE_URI", "tcp://0.0.0.0:10350")
    upstream = os.environ.get("GATE_UPSTREAM", "tcp://127.0.0.1:10305")
    require = str(os.environ.get("GATE_REQUIRE_MATCH", "true")).lower() in ("1", "true", "yes", "on")

    api_host = os.environ.get("API_HOST", "0.0.0.0")
    api_port = int(os.environ.get("API_PORT", "8088"))
    pcm_port = int(os.environ.get("DIALOGUE_PCM_PORT", "10500"))
    web_root = Path(os.environ.get("WEB_ROOT", "/app/web"))

    await start_api(store, runtime, api_host, api_port, web_root)

    await asyncio.gather(
        run_gate(gate_uri, upstream, store, require),
        run_pcm_server("0.0.0.0", pcm_port, runtime),
    )


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

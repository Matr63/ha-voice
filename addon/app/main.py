#!/usr/bin/env python3
"""HA Voice brain: satellite server (:10500), assistant, diary, optional Wyoming gate, HTTP API/UI."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import ha_install
from api import AppContext, start_api
from assistant import Assistant
from dialogue import DialogueRuntime, DialogueStore
from firmware import FirmwareStore
from gate_server import GateState, run_gate
from ha_client import HaClient
from modes import Modes
from satellite import SatelliteHub
from settings import SettingsStore
from speakers import SpeakerStore
from version import __version__

_LOGGER = logging.getLogger("ha-voice")


def _port(uri: str, default: int) -> int:
    try:
        return int(uri.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return default


async def amain() -> None:
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _LOGGER.info("HA Voice %s", __version__)

    settings = SettingsStore(Path(os.environ.get("SETTINGS_FILE", "/data/settings.json")))
    store = SpeakerStore(Path(os.environ.get("SPEAKERS_DIR", "/data/speakers")), settings)

    dialogues_dir = Path(os.environ.get("DIALOGUES_DIR", "/data/dialogues"))
    dialogue_store = DialogueStore(Path(os.environ.get("DIALOGUE_DB", str(dialogues_dir / "dialogue.db"))), dialogues_dir)
    stt_uri = os.environ.get("DIALOGUE_STT_URI", "tcp://127.0.0.1:10305")
    runtime = DialogueRuntime(store, dialogue_store, settings, stt_uri)
    gate_state = GateState()

    gate_uri = os.environ.get("GATE_URI", "tcp://0.0.0.0:10350")
    upstream = os.environ.get("GATE_UPSTREAM", stt_uri)
    api_port = int(os.environ.get("API_PORT", "8088"))
    pcm_port = int(os.environ.get("DIALOGUE_PCM_PORT", "10500"))

    tts_uri = os.environ.get("TTS_CLIENT_URI", "tcp://127.0.0.1:10351")
    tts_enabled = os.environ.get("TTS_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")
    ha = HaClient(settings)
    assistant = Assistant(settings, ha)
    modes = Modes(settings)
    hub = SatelliteHub(settings, runtime, assistant, stt_uri, tts_uri, modes, ha)
    firmware = FirmwareStore(Path(os.environ.get("FIRMWARE_DIR", "/data/firmware")))
    ctx = AppContext(
        settings=settings,
        store=store,
        runtime=runtime,
        gate=gate_state,
        hub=hub,
        assistant=assistant,
        ha=ha,
        firmware=firmware,
        stt_uri=stt_uri,
        stt_model=os.environ.get("STT_MODEL", "gigaam-v3-e2e-rnnt"),
        tts_uri=tts_uri,
        tts_enabled=tts_enabled,
        web_root=Path(os.environ.get("WEB_ROOT", "/app/web")),
        ports={
            "stt": _port(stt_uri, 10305),
            "gate": _port(gate_uri, 10350),
            "pcm": pcm_port,
            "api": api_port,
            "tts": _port(tts_uri, 10351),
        },
    )
    await start_api(ctx, os.environ.get("API_HOST", "0.0.0.0"), api_port)

    await asyncio.gather(
        run_gate(gate_uri, upstream, store, settings, gate_state),
        hub.serve("0.0.0.0", pcm_port),
        ha_install.run(api_port),
    )


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

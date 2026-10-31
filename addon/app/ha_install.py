"""Put the HA Voice integration into Home Assistant by itself (HAOS add-on only).

1. copy custom_components/ha_voice into the HA config dir when missing or older
2. announce the add-on through Supervisor discovery ("Обнаружено: HA Voice")
3. ask for a restart with a persistent notification when files changed
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

import aiohttp

from ha_client import supervisor_token

_LOGGER = logging.getLogger("ha-voice.install")

SOURCE = Path(os.environ.get("INTEGRATION_SRC", "/app/integration/ha_voice"))
HA_CONFIG_DIRS = (Path("/homeassistant"), Path("/config"))


def _version(path: Path) -> tuple[int, ...]:
    try:
        raw = json.loads((path / "manifest.json").read_text(encoding="utf-8")).get("version", "0")
        return tuple(int(x) for x in str(raw).split(".") if x.isdigit())
    except Exception:
        return (0,)


def copy_integration() -> bool:
    """Returns True when files were written and HA needs a restart."""
    if not SOURCE.is_dir():
        return False
    config = next((d for d in HA_CONFIG_DIRS if (d / "configuration.yaml").is_file()), None)
    if config is None:
        return False
    dest = config / "custom_components" / "ha_voice"
    if dest.is_dir() and _version(dest) >= _version(SOURCE):
        return False
    tmp = dest.with_name("ha_voice.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(SOURCE, tmp, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    _LOGGER.info("Integration ha_voice %s copied to %s", ".".join(map(str, _version(SOURCE))), dest)
    return True


async def _supervisor(session: aiohttp.ClientSession, method: str, path: str, payload: dict) -> None:
    token = supervisor_token()
    async with session.request(
        method, f"http://supervisor{path}", json=payload, headers={"Authorization": f"Bearer {token}"}
    ) as resp:
        if resp.status >= 400:
            raise RuntimeError(f"{path}: HTTP {resp.status} {await resp.text()}")


async def run(api_port: int) -> None:
    if not supervisor_token():
        from ha_client import token_diagnostics

        _LOGGER.warning("No Supervisor token %s — integration auto-install skipped", token_diagnostics())
        return  # docker compose: the integration is installed by hand / HACS
    try:
        changed = await asyncio.to_thread(copy_integration)
    except Exception:
        _LOGGER.exception("Could not copy the integration into Home Assistant")
        changed = False
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        if changed:
            try:
                await _supervisor(
                    session,
                    "POST",
                    "/core/api/services/persistent_notification/create",
                    {
                        "notification_id": "ha_voice_restart",
                        "title": "HA Voice",
                        "message": "Интеграция HA Voice установлена или обновлена. "
                        "Перезапустите Home Assistant — после этого она появится в «Обнаружено».",
                    },
                )
            except Exception as err:
                _LOGGER.warning("Notification failed: %s", err)
        try:
            await _supervisor(session, "POST", "/discovery", {"service": "ha_voice", "config": {"host": "127.0.0.1", "port": api_port}})
            _LOGGER.info("Supervisor discovery sent")
        except Exception as err:
            _LOGGER.info("Supervisor discovery not accepted (%s) — add the integration by hand, it finds the add-on itself", err)

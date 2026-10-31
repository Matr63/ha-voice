"""Home Assistant REST access: states, services, conversation agents.

On HAOS the add-on talks to Core through the Supervisor proxy with its own
token; with docker compose set HA URL + long-lived token in the panel.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from pathlib import Path

import aiohttp

from settings import SettingsStore

_LOGGER = logging.getLogger("ha-voice.ha")

# One template call returns every entity with its area, so the assistant knows
# "свет на кухне" without a WebSocket registry dump.
_ENTITIES_TEMPLATE = """
{%- set ns = namespace(rows=[]) -%}
{%- for s in states if s.domain in DOMAINS -%}
{%- set ns.rows = ns.rows + [[s.entity_id, s.name, s.state, area_name(s.entity_id) or '']] -%}
{%- endfor -%}
{{ ns.rows | tojson }}
"""

# The ESPHome device of a board (by MAC) and its area: HA uses the device's
# area for "включи свет" without a room.
_DEVICE_TEMPLATE = """
{%- set ns = namespace(d='') -%}
{%- for e in integration_entities('esphome') -%}
{%- if not ns.d -%}
{%- set d = device_id(e) -%}
{%- if d and (device_attr(d, 'connections') | string | lower).find(MAC) >= 0 -%}{%- set ns.d = d -%}{%- endif -%}
{%- endif -%}
{%- endfor -%}
{{ [ns.d, (area_name(ns.d) if ns.d else '') or ''] | tojson }}
"""
_AREAS_TEMPLATE = "{{ areas() | map('area_name') | list | tojson }}"
_AREA_DEVICE_TEMPLATE = "{{ (area_devices(AREA) | first) or '' }}"


class HaError(RuntimeError):
    pass


_TOKEN_KEYS = ("SUPERVISOR_TOKEN", "HASSIO_TOKEN")
_TOKEN_FILES = (
    "/run/s6/container_environment/SUPERVISOR_TOKEN",
    "/var/run/s6/container_environment/SUPERVISOR_TOKEN",
)


def _proc1_env() -> dict[str, str]:
    """The container's own environment (PID 1), in case a parent process dropped it."""
    try:
        raw = Path("/proc/1/environ").read_bytes()
    except OSError:
        return {}
    out = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            k, v = item.split(b"=", 1)
            out[k.decode(errors="replace")] = v.decode(errors="replace")
    return out


def supervisor_token() -> str | None:
    # HASSIO_TOKEN is the older name the Supervisor still sets next to SUPERVISOR_TOKEN
    for key in _TOKEN_KEYS:
        if os.environ.get(key):
            return os.environ[key]
    for path in _TOKEN_FILES:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    env1 = _proc1_env()
    for key in _TOKEN_KEYS:
        if env1.get(key):
            return env1[key]
    return None


def token_diagnostics() -> dict[str, Any]:
    env1 = _proc1_env()
    return {
        "env_tokens": sorted(k for k in os.environ if "TOKEN" in k),
        "pid1_tokens": sorted(k for k in env1 if "TOKEN" in k),
        "token_files": [p for p in _TOKEN_FILES if Path(p).exists()],
        "supervisor_env": sorted(k for k in os.environ if k.startswith(("SUPERVISOR", "HASSIO"))),
    }


class HaClient:
    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._device_id_ok = True

    def target(self) -> tuple[str, str] | None:
        s = self.settings.get()
        if s.ha_url and s.ha_token:
            return s.ha_url.rstrip("/") + "/api", s.ha_token
        token = supervisor_token()
        if token:
            return "http://supervisor/core/api", token
        return None

    @property
    def configured(self) -> bool:
        return self.target() is not None

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        return self._session

    async def request(self, method: str, path: str, payload: Any = None) -> Any:
        target = self.target()
        if target is None:
            raise HaError("Нет доступа к Home Assistant: укажите адрес и токен в настройках")
        base, token = target
        session = await self._http()
        try:
            async with session.request(
                method,
                base + path,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise HaError(f"Home Assistant {resp.status}: {text[:200]}")
                ctype = resp.headers.get("Content-Type", "")
                return json.loads(text) if "json" in ctype and text else text
        except aiohttp.ClientError as err:
            raise HaError(f"Home Assistant недоступен: {err}") from err

    async def ping(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "ok": False,
                "error": "не настроен: нет токена Supervisor и не заданы адрес/токен HA",
                **token_diagnostics(),
            }
        try:
            cfg = await self.request("GET", "/config")
            return {"ok": True, "version": cfg.get("version"), "location": cfg.get("location_name")}
        except HaError as err:
            return {"ok": False, "error": str(err)}

    async def entities(self, domains: list[str]) -> list[dict[str, str]]:
        tpl = _ENTITIES_TEMPLATE.replace("DOMAINS", json.dumps(domains))
        raw = await self.request("POST", "/template", {"template": tpl})
        rows = json.loads(raw) if isinstance(raw, str) else raw
        return [{"entity_id": r[0], "name": r[1], "state": r[2], "area": r[3]} for r in rows]

    async def _template_json(self, tpl: str) -> Any:
        raw = await self.request("POST", "/template", {"template": tpl})
        return json.loads(raw) if isinstance(raw, str) else raw

    async def device_for_mac(self, mac: str) -> tuple[str, str]:
        """(device_id, area name) of the board in HA's ESPHome integration, or ('', '')."""
        if not mac:
            return "", ""
        d, area = await self._template_json(_DEVICE_TEMPLATE.replace("MAC", json.dumps(mac.lower())))
        return d or "", area or ""

    async def areas(self) -> list[str]:
        return list(await self._template_json(_AREAS_TEMPLATE))

    async def area_device(self, area: str) -> str:
        """Any device in the area: passed as device_id it gives HA the room context."""
        raw = await self.request("POST", "/template", {"template": _AREA_DEVICE_TEMPLATE.replace("AREA", json.dumps(area))})
        return str(raw).strip()

    async def state(self, entity_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/states/{entity_id}")

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        return await self.request("POST", f"/services/{domain}/{service}", data)

    async def conversation(
        self,
        text: str,
        agent_id: str | None,
        conversation_id: str | None,
        language: str = "ru",
        device_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text, "language": language}
        if agent_id:
            payload["agent_id"] = agent_id
        if conversation_id:
            payload["conversation_id"] = conversation_id
        if device_id and self._device_id_ok:
            try:
                return await self.request("POST", "/conversation/process", {**payload, "device_id": device_id})
            except HaError as err:
                # older HA rejects unknown keys in this REST call: the room then
                # comes only from the phrase itself
                if " 400:" not in str(err):
                    raise
                _LOGGER.info("HA does not take device_id in /conversation/process: %s", err)
                self._device_id_ok = False
        return await self.request("POST", "/conversation/process", payload)

    async def agents(self) -> list[dict[str, str]]:
        states = await self.request("GET", "/states")
        out = [{"id": "", "name": "Home Assistant (встроенный)"}]
        for s in states:
            eid = s.get("entity_id", "")
            if eid.startswith("conversation.") and eid != "conversation.home_assistant":
                out.append({"id": eid, "name": s.get("attributes", {}).get("friendly_name") or eid})
        return out

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

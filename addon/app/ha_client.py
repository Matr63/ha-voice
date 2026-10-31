"""Home Assistant REST access: states, services, conversation agents.

On HAOS the add-on talks to Core through the Supervisor proxy with its own
token; with docker compose set HA URL + long-lived token in the panel.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

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


class HaError(RuntimeError):
    pass


def supervisor_token() -> str | None:
    # HASSIO_TOKEN is the older name the Supervisor still sets next to SUPERVISOR_TOKEN
    return os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN") or None


class HaClient:
    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None

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
            tokens = sorted(k for k in os.environ if "TOKEN" in k)
            return {
                "ok": False,
                "error": "не настроен: нет токена Supervisor и не заданы адрес/токен HA",
                "env_tokens": tokens,
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

    async def state(self, entity_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/states/{entity_id}")

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        return await self.request("POST", f"/services/{domain}/{service}", data)

    async def conversation(
        self, text: str, agent_id: str | None, conversation_id: str | None, language: str = "ru"
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"text": text, "language": language}
        if agent_id:
            payload["agent_id"] = agent_id
        if conversation_id:
            payload["conversation_id"] = conversation_id
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

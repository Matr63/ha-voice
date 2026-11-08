"""Home Assistant REST access: states, services, conversation agents.

On HAOS the add-on talks to Core through the Supervisor proxy with its own
token; with docker compose set HA URL + long-lived token in the panel.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from pathlib import Path

import aiohttp

from media_control import player_actions
from settings import SettingsStore
from tracing import active as _tracing, note as _note

_LOGGER = logging.getLogger("ha-voice.ha")

# One template call returns every entity with its area and device, so the
# assistant can tell two "Температура" sensors in the same room apart.
_ENTITIES_TEMPLATE = """
{%- set ns = namespace(rows=[]) -%}
{%- for s in states if s.domain in DOMAINS -%}
{%- set d = device_id(s.entity_id) -%}
{%- set dname = '' -%}
{%- if d -%}
{%- set dname = device_attr(d, 'name_by_user') or device_attr(d, 'name') or '' -%}
{%- endif -%}
{%- set ns.rows = ns.rows + [[s.entity_id, s.name, s.state, area_name(s.entity_id) or '', d or '', dname, s.attributes.get('unit_of_measurement') or '', s.attributes.get('supported_features') or 0]] -%}
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


# what one HA call puts into the command's trace
TRACE_LIST_ITEMS = 8
TRACE_CHARS = 3000


def _brief(value: Any) -> Any:
    """A long list (every entity of a domain) is shown by its count and first items."""
    if isinstance(value, list) and len(value) > TRACE_LIST_ITEMS:
        value = {"всего": len(value), "первые": value[:TRACE_LIST_ITEMS]}
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= TRACE_CHARS else text[:TRACE_CHARS] + "…"


def apply_time_zone(name: str | None) -> bool:
    """The add-on runs in UTC unless told: the time zone of Home Assistant is taken for it."""
    if not name or os.environ.get("TZ") == name or not hasattr(time, "tzset"):
        return False
    if not Path("/usr/share/zoneinfo", name).exists():
        _LOGGER.warning("Time zone %s not found in the image", name)
        return False
    os.environ["TZ"] = name
    time.tzset()
    _LOGGER.info("Time zone: %s", name)
    return True


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

    async def request(self, method: str, path: str, payload: Any = None, what: str = "", brief: Any = None) -> Any:
        """what: said in the trace instead of a long payload (a template).
        brief: put in the trace instead of the answer (the entity list lives in a snapshot)."""
        target = self.target()
        if target is None:
            raise HaError("Нет доступа к Home Assistant: укажите адрес и токен в настройках")
        base, token = target
        session = await self._http()
        t0 = time.monotonic()
        status = 0
        try:
            async with session.request(
                method,
                base + path,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                status = resp.status
                text = await resp.text()
                if resp.status >= 400:
                    raise HaError(f"Home Assistant {resp.status}: {text[:200]}")
                ctype = resp.headers.get("Content-Type", "")
                out = json.loads(text) if "json" in ctype and text else text
        except aiohttp.ClientError as err:
            self._trace(method, path, payload, what, t0, status, error=str(err))
            raise HaError(f"Home Assistant недоступен: {err}") from err
        except HaError as err:
            self._trace(method, path, payload, what, t0, status, error=str(err))
            raise
        self._trace(method, path, payload, what, t0, status, result=out if brief is None else brief)
        return out

    @staticmethod
    def _trace(
        method: str, path: str, payload: Any, what: str, t0: float, status: int, result: Any = None, error: str = ""
    ) -> None:
        if not _tracing():
            return
        data: dict[str, Any] = {"method": method, "path": path, "status": status, "seconds": round(time.monotonic() - t0, 2)}
        if what:
            data["what"] = what
        elif payload is not None:
            data["payload"] = _brief(payload)
        if error:
            data["error"] = error
        else:
            if isinstance(result, str) and what:
                try:
                    result = json.loads(result)
                except ValueError:
                    pass
            data["result"] = _brief(result)
        _note("ha_call", **data)

    async def ping(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "ok": False,
                "error": "не настроен: нет токена Supervisor и не заданы адрес/токен HA",
                **token_diagnostics(),
            }
        try:
            cfg = await self.request("GET", "/config")
            apply_time_zone(cfg.get("time_zone"))
            return {"ok": True, "version": cfg.get("version"), "location": cfg.get("location_name")}
        except HaError as err:
            return {"ok": False, "error": str(err)}

    async def entities(self, domains: list[str]) -> list[dict[str, str]]:
        tpl = _ENTITIES_TEMPLATE.replace("DOMAINS", json.dumps(domains))
        raw = await self.request(
            "POST", "/template", {"template": tpl}, what=f"все сущности доменов {', '.join(domains)}",
            brief="весь список — в снимке ниже",
        )
        rows = json.loads(raw) if isinstance(raw, str) else raw
        out = []
        for r in rows:
            eid = r[0]
            item = {
                "entity_id": eid,
                "name": r[1],
                "state": r[2],
                "area": r[3] if len(r) > 3 else "",
                "device_id": r[4] if len(r) > 4 else "",
                "device": r[5] if len(r) > 5 else "",
                "unit": r[6] if len(r) > 6 else "",
                "domain": eid.split(".", 1)[0],
            }
            if item["domain"] == "media_player":
                item["actions"] = player_actions(r[7] if len(r) > 7 else 0)
            out.append(item)
        return out

    async def _template_json(self, tpl: str, what: str = "шаблон") -> Any:
        raw = await self.request("POST", "/template", {"template": tpl}, what=what)
        return json.loads(raw) if isinstance(raw, str) else raw

    async def device_for_mac(self, mac: str) -> tuple[str, str]:
        """(device_id, area name) of the board in HA's ESPHome integration, or ('', '')."""
        if not mac:
            return "", ""
        d, area = await self._template_json(_DEVICE_TEMPLATE.replace("MAC", json.dumps(mac.lower())), f"устройство платы {mac}")
        return d or "", area or ""

    async def areas(self) -> list[str]:
        return list(await self._template_json(_AREAS_TEMPLATE, "список комнат"))

    async def area_device(self, area: str) -> str:
        """Any device in the area: passed as device_id it gives HA the room context."""
        raw = await self.request("POST", "/template", {"template": _AREA_DEVICE_TEMPLATE.replace("AREA", json.dumps(area))}, what=f"устройство в комнате {area}")
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

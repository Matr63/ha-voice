"""Home assistant brain: recognised text -> action in HA + a short spoken answer.

Backends:
  ha     - a Home Assistant conversation agent (built-in intents or any LLM
           agent configured in HA), via /api/conversation/process
  openai - any OpenAI-compatible chat API with tool calling (Ollama on this PC,
           LM Studio, OpenAI ...). Tools read states and call HA services and
           can ask a clarifying question ("на кухне или в зале?").
  off    - wake word only lights the ring, nothing is executed
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

import aiohttp

from ha_client import HaClient, HaError
from settings import SettingsStore

_LOGGER = logging.getLogger("ha-voice.assistant")

CONVERSATION_TTL_S = 300
MAX_TOOL_ROUNDS = 6
MAX_PROMPT_ENTITIES = 200
# Services the LLM may call; everything listed in settings can be read.
ACTION_DOMAINS = {
    "light", "switch", "fan", "cover", "climate", "media_player", "scene", "script",
    "input_boolean", "vacuum", "humidifier", "lock", "valve", "water_heater", "button", "input_button",
}

SYSTEM_PROMPT = """Ты — голосовой ассистент умного дома Home Assistant. Тебя слышат через колонку, ответ озвучивается.
Правила:
- Отвечай по-русски, коротко: одно-два предложения, без markdown, списков и эмодзи.
- Чтобы управлять устройствами, вызывай call_service. Чтобы узнать состояние — get_state или list_entities.
- Если непонятно, какое именно устройство имеется в виду (например, свет есть в нескольких комнатах), не угадывай — вызови ask_user с коротким уточняющим вопросом.
- После действия коротко подтверди, что сделал.
- Числа и единицы говори словами так, как их произносят.
Сейчас {now}. {speaker}
Устройства (entity_id | имя | комната | состояние):
{entities}
{extra}"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_entities",
            "description": "Список устройств и датчиков Home Assistant, можно отфильтровать по домену и комнате.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "например light, switch, sensor"},
                    "area": {"type": "string", "description": "название комнаты"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "Текущее состояние и атрибуты одной сущности.",
            "parameters": {
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_service",
            "description": "Вызвать сервис Home Assistant, например light.turn_on для light.kitchen.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "service": {"type": "string"},
                    "entity_id": {
                        "description": "одна сущность или список",
                        "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    },
                    "data": {"type": "object", "description": "доп. параметры, например brightness_pct"},
                },
                "required": ["domain", "service", "entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": "Задать пользователю уточняющий вопрос и дождаться ответа голосом.",
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    },
]


@dataclass
class AssistantResult:
    speech: str = ""
    expect_reply: bool = False
    backend: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class _Conversation:
    updated: float
    ha_conversation_id: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)


class Assistant:
    def __init__(self, settings: SettingsStore, ha: HaClient) -> None:
        self.settings = settings
        self.ha = ha
        self.log: deque[dict[str, Any]] = deque(maxlen=200)
        self._next_id = 1
        self._convs: dict[str, _Conversation] = {}
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------ api
    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self.log)[: max(1, limit)]

    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry["id"] = self._next_id
        self._next_id += 1
        entry.setdefault("ts", datetime.now().astimezone().isoformat(timespec="seconds"))
        self.log.appendleft(entry)
        return entry

    def forget(self, conv_key: str) -> None:
        self._convs.pop(conv_key, None)

    async def handle(self, text: str, conv_key: str, speaker: str | None = None) -> AssistantResult:
        s = self.settings.get()
        backend = s.assistant_backend
        conv = self._conv(conv_key)
        try:
            if backend == "off":
                result = AssistantResult(backend="off")
            elif backend == "openai":
                result = await self._llm(text, conv, speaker)
            else:
                result = await self._ha(text, conv)
        except (HaError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Assistant %s failed: %s", backend, err)
            result = AssistantResult(speech="Не получилось связаться с сервером.", backend=backend, error=str(err))
        except Exception as err:
            _LOGGER.exception("Assistant %s failed", backend)
            result = AssistantResult(speech="Что-то пошло не так.", backend=backend, error=str(err))
        if not result.expect_reply and backend == "openai":
            # keep the context briefly so "а теперь выключи" still works
            conv.messages = conv.messages[-12:]
        return result

    # ------------------------------------------------------------- helpers
    def _conv(self, key: str) -> _Conversation:
        now = time.time()
        for k in [k for k, c in self._convs.items() if now - c.updated > CONVERSATION_TTL_S]:
            del self._convs[k]
        conv = self._convs.get(key)
        if conv is None:
            conv = self._convs[key] = _Conversation(updated=now)
        conv.updated = now
        return conv

    def _domains(self) -> list[str]:
        return [d.strip() for d in self.settings.get().assistant_domains.split(",") if d.strip()]

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90))
        return self._session

    # ------------------------------------------------------------ backends
    async def _ha(self, text: str, conv: _Conversation) -> AssistantResult:
        s = self.settings.get()
        data = await self.ha.conversation(text, s.ha_agent_id or None, conv.ha_conversation_id)
        conv.ha_conversation_id = data.get("conversation_id") or conv.ha_conversation_id
        response = data.get("response") or {}
        speech = (((response.get("speech") or {}).get("plain") or {}).get("speech") or "").strip()
        targets = ((response.get("data") or {}).get("success") or [])
        actions = [{"target": t.get("id") or t.get("name"), "type": t.get("type")} for t in targets]
        expect = bool(data.get("continue_conversation")) or speech.rstrip().endswith("?")
        err = None
        if response.get("response_type") == "error":
            err = ((response.get("data") or {}).get("code")) or "error"
        return AssistantResult(speech=speech, expect_reply=expect, backend="ha", actions=actions, error=err)

    async def _entities_text(self) -> str:
        try:
            rows = await self.ha.entities(self._domains())
        except HaError as err:
            return f"(не удалось получить список: {err})"
        rows = rows[:MAX_PROMPT_ENTITIES]
        return "\n".join(f"{r['entity_id']} | {r['name']} | {r['area'] or '-'} | {r['state']}" for r in rows)

    async def _llm(self, text: str, conv: _Conversation, speaker: str | None) -> AssistantResult:
        s = self.settings.get()
        system = SYSTEM_PROMPT.format(
            now=datetime.now().strftime("%A %d.%m.%Y %H:%M"),
            speaker=f"Говорит: {speaker}." if speaker else "",
            entities=await self._entities_text(),
            extra=s.llm_prompt.strip(),
        )
        conv.messages.append({"role": "user", "content": text})
        result = AssistantResult(backend="openai")
        for _ in range(MAX_TOOL_ROUNDS):
            msg = await self._chat([{"role": "system", "content": system}, *conv.messages])
            calls = msg.get("tool_calls") or []
            conv.messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                result.speech = (msg.get("content") or "").strip()
                result.expect_reply = result.speech.endswith("?")
                return result
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if name == "ask_user":
                    question = str(args.get("question") or "").strip() or "Уточните, пожалуйста."
                    conv.messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": "asked"})
                    result.speech = question
                    result.expect_reply = True
                    return result
                output = await self._tool(name, args, result)
                conv.messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(output, ensure_ascii=False)}
                )
        result.speech = "Готово." if result.actions else "Не получилось разобраться с запросом."
        return result

    async def _chat(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        s = self.settings.get()
        headers = {"Content-Type": "application/json"}
        if s.llm_api_key:
            headers["Authorization"] = f"Bearer {s.llm_api_key}"
        body = {"model": s.llm_model, "messages": messages, "tools": TOOLS, "temperature": 0.2}
        session = await self._http()
        async with session.post(s.llm_url.rstrip("/") + "/chat/completions", json=body, headers=headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise HaError(f"LLM {resp.status}: {text[:200]}")
            data = json.loads(text)
        return (data.get("choices") or [{}])[0].get("message") or {}

    async def _tool(self, name: str, args: dict[str, Any], result: AssistantResult) -> Any:
        domains = self._domains()
        try:
            if name == "list_entities":
                dom = str(args.get("domain") or "").strip()
                area = str(args.get("area") or "").strip().lower()
                rows = await self.ha.entities([dom] if dom in domains else domains)
                if area:
                    rows = [r for r in rows if area in (r["area"] or "").lower()]
                return rows[:100]
            if name == "get_state":
                eid = str(args.get("entity_id") or "")
                if eid.split(".", 1)[0] not in domains:
                    return {"error": "эта сущность недоступна ассистенту"}
                st = await self.ha.state(eid)
                attrs = {k: v for k, v in (st.get("attributes") or {}).items() if not isinstance(v, (list, dict))}
                return {"entity_id": eid, "state": st.get("state"), "attributes": attrs}
            if name == "call_service":
                domain = str(args.get("domain") or "")
                service = str(args.get("service") or "")
                if domain not in ACTION_DOMAINS or domain not in domains:
                    return {"error": f"управление доменом {domain} не разрешено"}
                data = dict(args.get("data") or {})
                data["entity_id"] = args.get("entity_id")
                await self.ha.call_service(domain, service, data)
                result.actions.append({"service": f"{domain}.{service}", "target": args.get("entity_id"), "data": args.get("data")})
                return {"ok": True}
        except HaError as err:
            return {"error": str(err)}
        return {"error": f"неизвестная функция {name}"}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def result_dict(result: AssistantResult) -> dict[str, Any]:
    return asdict(result)

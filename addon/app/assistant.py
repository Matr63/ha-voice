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
KEEP_AUDIO = 40
MAX_TOOL_ROUNDS = 6
MAX_PROMPT_ENTITIES = 200
# HA answers that mean "didn't understand the phrase / no device by that name":
# with llm_fallback on, the LLM gets a second try at these.
FALLBACK_CODES = {"no_intent_match", "no_valid_targets", "no_intent", "failed_to_handle"}
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
- Текущие значения датчиков и состояния устройств узнавай через get_state.
- В начале сообщения пользователя в квадратных скобках — время, кто говорит и комната колонки.
Устройства (entity_id | имя | комната):
{entities}
{extra}"""
# The system prompt holds nothing that changes between requests (no time, no
# states): llama.cpp then reuses its cache and reads only the new words, instead
# of the whole device list on every command (tens of seconds on a CPU).

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
        # command audio by log id: "чей это голос?" in the log teaches the voice
        self.audio: dict[int, bytes] = {}
        # how long answers take (for "обычно ~N с") and what is going on right now
        self.durations: deque[float] = deque(maxlen=10)
        self.busy: dict[str, Any] | None = None
        self._convs: dict[str, _Conversation] = {}
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------ api
    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self.log)[: max(1, limit)]

    def record(self, entry: dict[str, Any], pcm: bytes | None = None) -> dict[str, Any]:
        entry["id"] = self._next_id
        self._next_id += 1
        if pcm:
            self.audio[entry["id"]] = pcm
            entry["has_audio"] = True
            for old in sorted(self.audio)[:-KEEP_AUDIO]:
                del self.audio[old]
        entry.setdefault("ts", datetime.now().astimezone().isoformat(timespec="seconds"))
        self.log.appendleft(entry)
        return entry

    def typical_s(self) -> float | None:
        if not self.durations:
            return None
        return round(sorted(self.durations)[len(self.durations) // 2], 1)

    def _stage(self, text: str) -> None:
        if self.busy is not None:
            self.busy["stage"] = text

    def forget(self, conv_key: str) -> None:
        self._convs.pop(conv_key, None)

    async def handle(
        self,
        text: str,
        conv_key: str,
        speaker: str | None = None,
        device_id: str | None = None,
        room: str | None = None,
    ) -> AssistantResult:
        s = self.settings.get()
        backend = s.assistant_backend
        conv = self._conv(conv_key)
        t0 = time.monotonic()
        self.busy = {"since": time.time(), "stage": "Home Assistant" if backend == "ha" else "LLM"}
        try:
            if backend == "off":
                result = AssistantResult(backend="off")
            elif backend == "openai":
                result = await self._llm(text, conv, speaker, room)
            else:
                result = await self._ha(text, conv, device_id)
                if result.error in FALLBACK_CODES and s.llm_fallback:
                    self._stage("HA не понял — спрашиваю LLM")
                    result = await self._fallback(text, conv, speaker, result, room)
        except (HaError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Assistant %s failed: %s", backend, err)
            result = AssistantResult(speech="Не получилось связаться с сервером.", backend=backend, error=str(err))
        except Exception as err:
            _LOGGER.exception("Assistant %s failed", backend)
            result = AssistantResult(speech="Что-то пошло не так.", backend=backend, error=str(err))
        self.busy = None
        if backend != "off":
            self.durations.append(time.monotonic() - t0)
        if not result.expect_reply and result.backend.endswith("openai"):
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
    async def _ha(self, text: str, conv: _Conversation, device_id: str | None = None) -> AssistantResult:
        s = self.settings.get()
        data = await self.ha.conversation(text, s.ha_agent_id or None, conv.ha_conversation_id, device_id=device_id)
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

    async def _fallback(
        self, text: str, conv: _Conversation, speaker: str | None, ha_result: AssistantResult, room: str | None = None
    ) -> AssistantResult:
        """HA did not understand: let the LLM try. If it is not reachable, keep HA's answer."""
        keep = len(conv.messages)
        try:
            result = await self._llm(text, conv, speaker, room)
        except (HaError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.info("LLM fallback unavailable: %s", err)
            del conv.messages[keep:]
            return ha_result
        result.backend = "ha+openai"
        if not result.speech and not result.actions:
            return ha_result
        return result

    async def _entities_text(self) -> str:
        try:
            rows = await self.ha.entities(self._domains())
        except HaError as err:
            return f"(не удалось получить список: {err})"
        rows = sorted(rows, key=lambda r: r["entity_id"])[:MAX_PROMPT_ENTITIES]
        return "\n".join(f"{r['entity_id']} | {r['name']} | {r['area'] or '-'}" for r in rows)

    async def _system(self) -> str:
        return SYSTEM_PROMPT.format(entities=await self._entities_text(), extra=self.settings.get().llm_prompt.strip())

    def _local_llm(self) -> bool:
        return self.settings.get().llm_url.rstrip("/").startswith("http://127.0.0.1:10360")

    async def prewarm(self) -> None:
        """Read the system prompt into the local model's cache before the first command."""
        if not self._local_llm():
            return
        t0 = time.monotonic()
        try:
            await self._chat(
                [{"role": "system", "content": await self._system()}, {"role": "user", "content": "привет"}], max_tokens=1
            )
            _LOGGER.info("LLM prompt cached in %.1f s", time.monotonic() - t0)
        except Exception as err:
            _LOGGER.info("LLM prewarm failed: %s", err)

    async def _llm(self, text: str, conv: _Conversation, speaker: str | None, room: str | None = None) -> AssistantResult:
        ctx = [datetime.now().strftime("%A %d.%m.%Y %H:%M")]
        if speaker:
            ctx.append(f"говорит {speaker}")
        if room:
            ctx.append(f"колонка в комнате «{room}» — если комната не названа, речь о ней")
        system = await self._system()
        conv.messages.append({"role": "user", "content": f"[{'; '.join(ctx)}]\n{text}"})
        result = AssistantResult(backend="openai")
        for step in range(MAX_TOOL_ROUNDS):
            self._stage("LLM думает" if step == 0 else f"LLM думает (шаг {step + 1})")
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
                self._stage(f"выполняю {args.get('domain', '')}.{args.get('service', '')}" if name == "call_service" else f"смотрю {name}")
                output = await self._tool(name, args, result)
                conv.messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(output, ensure_ascii=False)}
                )
        result.speech = "Готово." if result.actions else "Не получилось разобраться с запросом."
        return result

    async def _chat(self, messages: list[dict[str, Any]], max_tokens: int = 300) -> dict[str, Any]:
        s = self.settings.get()
        headers = {"Content-Type": "application/json"}
        if s.llm_api_key:
            headers["Authorization"] = f"Bearer {s.llm_api_key}"
        body: dict[str, Any] = {
            "model": s.llm_model, "messages": messages, "tools": TOOLS, "temperature": 0.2, "max_tokens": max_tokens,
        }
        if self._local_llm():
            body["cache_prompt"] = True
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

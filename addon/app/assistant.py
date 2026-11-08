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
import re
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from difflib import get_close_matches, ndiff
from pathlib import Path
from typing import Any, Callable

import aiohttp

from entity_snapshots import EntitySnapshots
from ha_client import HaClient, HaError
from memory import Memory
from media_control import service_data, service_name, supports, twins, without_unnamed_twins
from notes import HOLD_OPEN_S, HOLD_SETTLE_S, NOTES_RULES, NOTES_TOOLS, NotesBook, Reply, in_domain
from plugins import PLUGIN_TOOL_NAMES
from sensor_lookup import MAX_ANSWERS, WEATHER_ATTRS, asked_measure, asks_outside, find_sensors, spoken_value
from settings import SettingsStore
from tracing import note as _note, start as _start_trace

_LOGGER = logging.getLogger("ha-voice.assistant")

CONVERSATION_TTL_S = 300
KEEP_AUDIO = 40
KEEP_TRACES = 40
# The time goes to the model again only after this long: within a talk it barely moves
CONTEXT_TIME_RESEND_S = 300
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


MAX_TOOL_ROUNDS = 6
MAX_RECOVERED_READS = 6
MAX_SPOKEN_READS = 3
MAX_PROMPT_ENTITIES = 200
ENTITIES_CACHE_S = 30
# HA answers that mean "didn't understand the phrase / no device by that name":
# with llm_fallback on, the LLM gets a second try at these.
FALLBACK_CODES = {"no_intent_match", "no_valid_targets", "no_intent", "failed_to_handle"}
# Services the LLM may call; everything listed in settings can be read.
ACTION_DOMAINS = {
    "light", "switch", "fan", "cover", "climate", "media_player", "scene", "script",
    "input_boolean", "vacuum", "humidifier", "lock", "valve", "water_heater", "button", "input_button",
}

SYSTEM_RULES = """Ты — голосовой ассистент умного дома Home Assistant. Тебя слышат через колонку, ответ озвучивается.
Правила:
- Отвечай по-русски, коротко: одно-два предложения, без markdown, списков и эмодзи.
- Чтобы управлять устройствами, вызывай call_service. Чтобы узнать состояние — get_state или list_entities.
- Функции вызывай только как вызов функции. В тексте ответа никогда не пиши их названия и entity_id и не копируй строки списка устройств: текст читается вслух.
- Нужное ищи так: сначала комната (названная, совпавшая с тем, что после «она же», иначе комната колонки), затем в ней датчик или устройство по имени и подсказкам.
- Спрашивают значение, а подходящих датчиков несколько — прочитай один, с совпавшей подсказкой, и назови его значение; не перечисляй их и не переспрашивай.
- Предлог важен: «у холодильника» — рядом с ним, «в холодильнике» — внутри него. Если подходящего датчика нет, так и скажи и не подставляй соседний.
- Если непонятно, каким устройством управлять (например, свет есть в нескольких комнатах), не угадывай — вызови ask_user с коротким уточняющим вопросом.
- Никогда не повторяй вопрос пользователя и не задавай его обратно — отвечай на него. ask_user — только чтобы уточнить, какое устройство или комнату имели в виду. Не знаешь ответа — так и скажи.
- Если в сообщении есть блок «Память», это недавний разговор: на вопросы о сказанном раньше («сколько ты говорила варить гречку») отвечай по нему, ничего не вызывая.
- После действия коротко подтверди, что сделал.
- Числа и единицы говори словами так, как их произносят.
- Текущие значения датчиков и состояния устройств узнавай через get_state.
- Никогда не отвечай «сейчас узнаю», «посмотрю», «проверю»: второго шанса не будет. Сразу вызови функцию и ответь результатом.
- Одинаковые имена датчиков в одной комнате — это разные приборы; смотри имя прибора и подсказки.
- В начале сообщения пользователя в квадратных скобках бывает контекст: time — время, speaker — кто говорит, room — комната колонки. Он действует, пока не придёт новый. Если комната в запросе не названа, речь о комнате колонки."""
CATALOG_HEADER = "Справочник устройств: комната, в ней прибор, в нём entity_id — имя (подсказки)."
# The system prompt holds nothing that changes between requests (no time, no
# states): llama.cpp then reuses its cache and reads only the new words, instead
# of the whole device list on every command (tens of seconds on a CPU).
PROMPT_NAMES = {"home": "дом", "notes": "списки и заметки", "list_review": "проверка списка", "list_match": "поиск похожего"}
# llama-server slots: each prompt keeps its own place in the cache
SLOT_HOME = 0
SLOT_NOTES = 1
# a plugin reading a page: a long prompt, new every time, kept away from the other two
SLOT_PLUGINS = 2
# reading a page on a CPU takes far longer than a command
PLUGIN_TIMEOUT_S = 600
# how long a question back waits for its answer; later the phrase is a new command
PENDING_TTL_S = 45
# «а в зале?» right after «температура на кухне»
FOLLOWUP_TTL_S = 120
MAX_DIFF_LINES = 60
# the house quiet this long: the model writes the memory's summaries
MEMORY_IDLE_S = 20
MEMORY_RULES = (
    "Сожми ответ голосового ассистента в одну короткую фразу, до 20 слов. Сохрани числа, сроки, названия и суть; "
    "без вступлений. Вызови remember."
)
REMEMBER_TOOL = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": "Сохранить краткое содержание ответа.",
        "parameters": {
            "type": "object",
            "properties": {"summary": {"type": "string", "description": "Одна фраза: суть ответа с числами."}},
            "required": ["summary"],
        },
    },
}
# Before a dictated list is written, the model reads it once more and drops what
# is not an item. In English: shorter for the model, so faster on a CPU. Constant,
# so llama.cpp keeps it cached; the list itself goes in the user message.
LIST_REVIEW_RULES = """You check a list that was dictated by voice and transcribed by speech recognition, before it is saved.
The user message gives the list name and its numbered items (in Russian).
Remove an item when it is not something to put on such a list:
- filler words, interjections, hesitations;
- yes/no answers, confirmations, agreement or refusal;
- words addressed to the assistant or to other people, pieces of conversation;
- recognition noise: a fragment or a word that means nothing on its own;
- a repeat of another item.
Keep every real item, even an unusual one or one that does not match the list name. When in doubt, keep it.
Never change, merge or add items. Call keep_items with the numbers of the items to keep."""
LIST_REVIEW_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "keep_items",
            "description": "The numbers of the items that stay on the list.",
            "parameters": {
                "type": "object",
                "properties": {"keep": {"type": "array", "items": {"type": "integer"}}},
                "required": ["keep"],
            },
        },
    }
]
# The code did not find a list or an item by its words («пакет молока» when the
# list has «бутылка молока», «продукты» for «покупки»): the model matches by meaning.
LIST_MATCH_RULES = """A person named things by voice (speech recognition, Russian); the code did not find them among the options.
The user message gives the numbered options and the numbered things said.
For each thing said, pick the option the person most likely meant: the same thing in other words, another form, a synonym,
the same product in another package or amount, a misheard word. Pick 0 when no option fits; do not pick between two different things.
Call match with one number per thing said, in order."""
LIST_MATCH_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "match",
            "description": "For each thing said, in order: the number of the option, or 0.",
            "parameters": {
                "type": "object",
                "properties": {"picks": {"type": "array", "items": {"type": "integer"}}},
                "required": ["picks"],
            },
        },
    }
]
SAVING_PHRASE = "Записываю"

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
            "description": (
                "Вызвать сервис Home Assistant, например light.turn_on для light.kitchen. "
                "Сервисы media_player: turn_on, turn_off, media_play, media_pause, media_stop, media_next_track, "
                "media_previous_track, volume_up, volume_down, volume_mute, "
                "volume_set с data.volume_level в процентах от 0 до 100."
            ),
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

# Small local models sometimes write a tool call as plain text instead of making
# it: get_state(sensor.x), light.turn_on(entity_id="light.y"), {"name": ..., "arguments": ...}
_TOOL_NAMES = sorted([t["function"]["name"] for t in TOOLS] + PLUGIN_TOOL_NAMES)
_TOOL_POSITIONAL = {
    "get_state": ("entity_id",),
    "ask_user": ("question",),
    "list_entities": ("domain", "area"),
    "call_service": ("domain", "service", "entity_id"),
}
_TEXT_ARGS = r"\(((?:\"[^\"]*\"|'[^']*'|[^()\"'])*)\)"
_TEXT_TOOL_RE = re.compile(rf"\b({'|'.join(_TOOL_NAMES)})\s*{_TEXT_ARGS}")
_TEXT_SERVICE_RE = re.compile(rf"\b({'|'.join(sorted(ACTION_DOMAINS))})\.([a-z0-9_]+)\s*{_TEXT_ARGS}")
_TEXT_ARG_RE = re.compile(r"""(?:"[^"]*"|'[^']*'|[^,"'])+""")
_TEXT_KWARG_RE = re.compile(r"(\w+)\s*[=:]\s*(.+)", re.S)


def _parse_text_args(name: str, raw: str) -> dict[str, Any]:
    positional: list[str] = []
    args: dict[str, Any] = {}
    for part in _TEXT_ARG_RE.findall(raw):
        part = part.strip()
        if not part:
            continue
        kw = _TEXT_KWARG_RE.fullmatch(part)
        if kw:
            args[kw.group(1)] = kw.group(2).strip().strip("\"'")
        else:
            positional.append(part.strip("\"'"))
    if name == "call_service":
        # call_service(light.turn_on, light.kitchen)
        if positional and "." in positional[0]:
            positional[:1] = positional[0].split(".", 1)
        elif "." in str(args.get("service") or ""):
            args["domain"], args["service"] = args["service"].split(".", 1)
    for key, value in zip(_TOOL_POSITIONAL.get(name, ()), positional):
        args.setdefault(key, value)
    return args


def _say_dropped(reply: Reply, said: list[str], kept: list[str]) -> Reply:
    """The model took something off a dictated list: say so, to be added again if it was real."""
    dropped = [item for item in said if item not in kept]
    if dropped and not reply.speech:
        reply.speech = f"Не записала: {', '.join(dropped)}."
    elif dropped:
        head, ask = reply.speech, ""
        if head.endswith(" Ещё что-нибудь?"):
            head, ask = head[: -len(" Ещё что-нибудь?")], " Ещё что-нибудь?"
        reply.speech = f"{head} Не записала: {', '.join(dropped)}.{ask}"
    return reply


def _json_tool_calls(content: str) -> list[tuple[str, dict[str, Any]]]:
    decoder = json.JSONDecoder()
    found: list[tuple[str, dict[str, Any]]] = []
    start = content.find("{")
    while start >= 0:
        try:
            obj, end = decoder.raw_decode(content, start)
        except ValueError:
            start = content.find("{", start + 1)
            continue
        if isinstance(obj, dict) and obj.get("name") in _TOOL_NAMES:
            args = obj.get("arguments") or obj.get("parameters") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            found.append((obj["name"], args if isinstance(args, dict) else {}))
        start = content.find("{", end)
    return found


def text_tool_calls(content: str) -> list[dict[str, Any]]:
    """Tool calls written in the reply text, in the shape of message.tool_calls."""
    found = _json_tool_calls(content)
    if not found:
        found = [(m.group(1), _parse_text_args(m.group(1), m.group(2))) for m in _TEXT_TOOL_RE.finditer(content)]
    if not found:
        for m in _TEXT_SERVICE_RE.finditer(content):
            args = _parse_text_args("get_state", m.group(3))
            found.append(("call_service", {**args, "domain": m.group(1), "service": m.group(2)}))
    return [
        {
            "id": f"text_{i}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        }
        for i, (name, args) in enumerate(found)
    ]


# "Я узнаю, Саша." with no call made: the model promised to look and stopped.
# Nobody asks again, so it is told once to make the call now.
_PROMISE_RE = re.compile(
    r"\b(?:узна(?:ю|ем)|посмотр(?:ю|им)|провер(?:ю|им)|уточн(?:ю|им)|выясн(?:ю|им)|гляну|подожди(?:те)?)\b",
    re.IGNORECASE,
)
PROMISE_NUDGE = "(Не обещай. Вызови нужную функцию прямо сейчас и ответь результатом.)"
# «двадцать четыре и десять десятых градуса» with nothing read: a value written in
# words, as the rules ask, is made up just the same as one written in digits
_SPOKEN_VALUE_RE = re.compile(r"\b(?:градус|процент|гектопаскал|люкс|миллиметр)", re.IGNORECASE)


def is_promise(content: str) -> bool:
    return bool(_PROMISE_RE.search(content))


# «Не могу определить, какой телевизор» with no call made. Left in the history it comes
# back word for word on every next command, so a refused exchange is not kept there.
_REFUSAL_RE = re.compile(
    r"\bне\s+(?:мог(?:у|ла)\b|удал|получ|найд|нашл)|\bпроизошла\s+ошибк|\bпопробуйте\b|\bукажите\b",
    re.IGNORECASE,
)
# «Видеоролик воспроизведён» with no call made: nothing happened, whatever the model says
_CLAIM_RE = re.compile(
    r"\b(?:вы?ключ(?:[её]н|ил)|воспроизв(?:[её]д|ож)|запустил|запущен|останов(?:ил|лен)|постав(?:ил|лен)|на\s+паузе)",
    re.IGNORECASE,
)
UNBACKED_NUDGE = (
    "(Функцию ты не вызвал, значит ничего не сделано. Если это команда устройству, вызови call_service "
    "с entity_id из справочника. Если это не команда, ответь коротко.)"
)


ECHO_NUDGE = (
    "(Ты повторил вопрос пользователя — так нельзя. Если вопрос о доме, вызови нужную функцию. "
    "Если нет — ответь сам одной-двумя фразами или скажи, что не знаешь.)"
)
_ECHO_FILLER = {"а", "и", "ну", "еще", "раз", "почти", "скажи", "подскажи", "пожалуйста", "мне"}


def _echo_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[а-яa-z0-9]+", text.lower().replace("ё", "е")) if w not in _ECHO_FILLER}


def is_echo(question: str, text: str) -> bool:
    """«Какая температура в зале?» asked back to «Какая погода в зале? Какая температура в зале?»:
    the model repeats the user's question instead of answering it."""
    asked = _echo_words(question)
    said = _echo_words(text)
    return len(asked) >= 2 and len(asked & said) >= 0.8 * len(asked)


def _asked_back(calls: list[dict[str, Any]], content: str) -> str:
    """The question the model puts to the user: an ask_user call or a plain text question."""
    for call in calls:
        fn = call.get("function") or {}
        if fn.get("name") == "ask_user":
            try:
                return str(json.loads(fn.get("arguments") or "{}").get("question") or "")
            except (json.JSONDecodeError, AttributeError):
                return ""
    return content if not calls and content.endswith("?") else ""


def is_refusal(content: str) -> bool:
    return bool(_REFUSAL_RE.search(content))


def is_claim(content: str) -> bool:
    return not content.endswith("?") and bool(_CLAIM_RE.search(content))


@dataclass
class AssistantResult:
    speech: str = ""
    expect_reply: bool = False
    backend: str = ""
    actions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    # seconds to keep the mic open after this answer; None uses the usual follow-up wait
    hold_s: float | None = None
    # False: the pause already running is not restarted (another voice, or talk that is not the list)
    extend: bool = True


@dataclass
class _Conversation:
    updated: float
    ha_conversation_id: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    # a question about a list waiting for its answer
    pending: dict[str, Any] | None = None
    pending_until: float = 0.0
    # said before the next answer when a list was written because the talk moved on
    list_prefix: str = ""
    # what the last direct answer measured, for «а в зале?»
    last_measure: str = ""
    last_measure_at: float = 0.0
    # a plugin answered last, for «а завтра?»
    last_plugin_at: float = 0.0
    # «Что поискать?»: the next phrase is the question for this plugin
    plugin_wait: str = ""
    plugin_wait_at: float = 0.0


class Assistant:
    def __init__(self, settings: SettingsStore, ha: HaClient, notes: NotesBook | None = None) -> None:
        self.settings = settings
        self.ha = ha
        self.notes = notes or NotesBook(Path("/data/notes.json"))
        # the prompt last sent, per prompt: the log shows only what changed
        self._last_prompts: dict[str, str] = {}
        self._cached_notes: str | None = None
        self.log: deque[dict[str, Any]] = deque(maxlen=200)
        self._next_id = 1
        # command audio by log id: "чей это голос?" in the log teaches the voice
        self.audio: dict[int, bytes] = {}
        self.traces: dict[int, list[dict[str, Any]]] = {}
        # how long answers take (for "обычно ~N с") and what is going on right now
        self.durations: deque[float] = deque(maxlen=10)
        # the LLM alone: what the next answer is predicted from
        self.llm_durations: deque[float] = deque(maxlen=10)
        self._cached_system: str | None = None
        self._prewarm_task: asyncio.Task | None = None
        self.busy: dict[str, Any] | None = None
        self._convs: dict[str, _Conversation] = {}
        self._session: aiohttp.ClientSession | None = None
        self.hub: Any = None
        self.plugins: Any = None  # PluginManager
        self._ent_cache: tuple[float, list[dict[str, str]]] | None = None
        self._ent_snapshot = 0
        self.memory = Memory(Path("/data/memory.json") if Path("/data").is_dir() else None)
        self._memory_task: asyncio.Task | None = None
        self.snapshots = EntitySnapshots(Path("/data/entity_snapshots.json") if Path("/data").is_dir() else None)

    # ------------------------------------------------------------------ api
    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self.log)[: max(1, limit)]

    def record(
        self, entry: dict[str, Any], pcm: bytes | None = None, trace: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        entry["id"] = self._next_id
        self._next_id += 1
        if trace:
            self.traces[entry["id"]] = trace
            entry["has_trace"] = True
            for old in sorted(self.traces)[:-KEEP_TRACES]:
                del self.traces[old]
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
        on_llm: Callable[[float | None], None] | None = None,
        speaker_id: str | None = None,
        on_saving: Callable[[str], None] | None = None,
    ) -> AssistantResult:
        """on_llm: called when the LLM is about to start, with the seconds it is expected to take.
        on_saving: says a phrase at once («Записываю») while a dictated list is checked."""
        s = self.settings.get()
        backend = s.assistant_backend
        conv = self._conv(conv_key)
        t0 = time.monotonic()
        # a summary being written for the memory waits: the answer comes first
        self._stop_memory()
        self.busy = {"since": time.time(), "stage": "Home Assistant" if backend == "ha" else "LLM"}
        trace = _start_trace()
        _note("input", text=text, backend=backend, room=room, speaker=speaker, device_id=device_id)
        try:
            notes = await self._plugin_trigger(text, conv, on_llm) if backend != "off" else None
            if notes is None and backend != "off" and s.notes_enabled:
                notes = await self._notes(text, conv, speaker, on_llm, speaker_id, on_saving)
            if notes is None and backend != "off":
                notes = self._plugin_answer(text, conv)
            if notes is not None:
                result = notes
            elif backend == "off":
                result = AssistantResult(backend="off")
            elif backend == "openai":
                result = await self._llm(text, conv, speaker, room, on_llm)
            else:
                result = await self._ha(text, conv, device_id)
                if result.error in FALLBACK_CODES and s.llm_fallback:
                    self._stage("HA не понял — спрашиваю LLM")
                    result = await self._fallback(text, conv, speaker, result, room, on_llm)
        except (HaError, aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Assistant %s failed: %s", backend, err)
            result = AssistantResult(speech="Не получилось связаться с сервером.", backend=backend, error=str(err))
            conv.list_prefix = ""
        except Exception as err:
            _LOGGER.exception("Assistant %s failed", backend)
            result = AssistantResult(speech="Что-то пошло не так.", backend=backend, error=str(err))
            conv.list_prefix = ""
        self.busy = None
        result.trace = trace
        if result.error:
            _note("error", error=result.error)
        if backend != "off":
            self.durations.append(time.monotonic() - t0)
        if not result.expect_reply and result.backend.endswith(("openai", "direct")):
            # keep the context briefly so "а теперь выключи" still works; it starts
            # at a phrase: a tool answer without its call confuses the model
            conv.messages = _trim_history(conv.messages)
        prefix = conv.list_prefix
        conv.list_prefix = ""
        if prefix:
            result.speech = f"{prefix} {result.speech}".strip()
        if backend != "off" and result.speech and not result.error and not result.expect_reply:
            self.memory.add(text, result.speech, speaker or "", result.backend)
        self.schedule_memory()
        return result

    # ------------------------------------------------------------- memory
    def _stop_memory(self) -> None:
        if self._memory_task is not None and not self._memory_task.done():
            self._memory_task.cancel()
        self._memory_task = None

    def schedule_memory(self, delay_s: float = MEMORY_IDLE_S) -> None:
        """Summaries of long answers, written while nobody talks; the next command cancels it."""
        self._stop_memory()
        s = self.settings.get()
        if not self.memory.pending() or not s.llm_url or s.assistant_backend == "off":
            return

        async def later() -> None:
            await asyncio.sleep(delay_s)
            await self.compress_memory()

        self._memory_task = asyncio.create_task(later())

    async def compress_memory(self) -> int:
        """One phrase per long answer, oldest first. Stops at the first failure: tried again after the next command."""
        done = 0
        for item in self.memory.pending():
            if self.busy is not None:
                break
            messages = [
                {"role": "system", "content": MEMORY_RULES},
                {"role": "user", "content": f"Вопрос: {item.question}\nОтвет: {item.answer}"},
            ]
            try:
                msg = await self._chat(
                    messages, max_tokens=120, tool_choice="required", tools=[REMEMBER_TOOL], slot=SLOT_PLUGINS,
                    timeout=PLUGIN_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                raise
            except Exception as err:
                _LOGGER.info("Memory summary not written: %s", err)
                break
            summary = ""
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    summary = str(json.loads(fn.get("arguments") or "{}").get("summary") or "")
                except (json.JSONDecodeError, AttributeError):
                    pass
            summary = re.sub(r"\s+", " ", summary or (msg.get("content") or "")).strip()
            if not summary:
                break
            item.summary = summary[:300]
            done += 1
            _LOGGER.info("Memory: «%s» -> «%s»", item.question, item.summary)
        if done:
            self.memory.save()
        return done

    def _memory_block(self, conv: _Conversation, keep: int) -> str:
        """What was said lately and is not in the talk the model already has."""
        in_talk = [m.get("_text", "") for m in conv.messages[:keep] if m.get("role") == "user"]
        block = self.memory.block(skip=in_talk)
        if block:
            _note("memory", lines=block.count("\n- "), chars=len(block))
        return block

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

    def invalidate_entities(self) -> None:
        self._ent_cache = None

    def _catalog(self) -> dict[str, Any]:
        try:
            data = json.loads(self.settings.get().entity_catalog or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def room_hints(self) -> dict[str, str]:
        """What a room is also called: {"Кухня": "столешница, у холодильника"}."""
        try:
            data = json.loads(self.settings.get().room_hints or "{}")
        except (TypeError, ValueError):
            return {}
        return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else {}

    def _board_ids(self) -> set[str]:
        hub = self.hub
        if hub is None:
            return set()
        return {s.board_device_id for s in hub.satellites.values() if getattr(s, "board_device_id", None)}

    def _exposed(self, row: dict[str, str], catalog: dict[str, Any] | None = None) -> bool:
        catalog = self._catalog() if catalog is None else catalog
        meta = catalog.get(row["entity_id"])
        if isinstance(meta, dict) and "enabled" in meta:
            return bool(meta["enabled"])
        did = row.get("device_id") or ""
        return not (did and did in self._board_ids())

    def _hints(self, entity_id: str, catalog: dict[str, Any] | None = None) -> str:
        catalog = self._catalog() if catalog is None else catalog
        meta = catalog.get(entity_id)
        if isinstance(meta, dict):
            return str(meta.get("hints") or "").strip()
        return ""

    async def _all_rows(self) -> list[dict[str, str]]:
        now = time.monotonic()
        if self._ent_cache and now - self._ent_cache[0] < ENTITIES_CACHE_S:
            # the catalog may have changed since: what the model gets is in the snapshot
            self._snapshot(self._ent_cache[1], quiet=True)
            _note(
                "ha_cache", what="список сущностей", count=len(self._ent_cache[1]),
                age_s=round(now - self._ent_cache[0], 1), snapshot=self._ent_snapshot,
            )
            return self._ent_cache[1]
        rows = await self.ha.entities(self._domains())
        self._ent_cache = (now, rows)
        self._snapshot(rows)
        return rows

    def _snapshot(self, rows: list[dict[str, str]], quiet: bool = False) -> None:
        """The list goes to its own table; the command log only links to it."""
        catalog = self._catalog()
        marked = [{**r, "exposed": self._exposed(r, catalog)} for r in rows]
        sid, new = self.snapshots.add(marked)
        self._ent_snapshot = sid
        if not quiet:
            _note(
                "entities", snapshot=sid, new=new, count=len(rows), exposed=sum(1 for r in marked if r["exposed"])
            )

    async def catalog_view(self) -> list[dict[str, Any]]:
        """Devices with their entities, overlaying enabled/hints from settings."""
        rows = await self._all_rows()
        catalog = self._catalog()
        board_ids = self._board_ids()
        groups: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for r in rows:
            did = r.get("device_id") or f"entity:{r['entity_id']}"
            if did not in groups:
                groups[did] = {
                    "id": did,
                    "name": r.get("device") or r["name"],
                    "area": r.get("area") or "",
                    "board": bool(r.get("device_id") and r["device_id"] in board_ids),
                    "entities": [],
                }
                order.append(did)
            elif r.get("device") and not groups[did]["name"]:
                groups[did]["name"] = r["device"]
            if r.get("area") and not groups[did]["area"]:
                groups[did]["area"] = r["area"]
            groups[did]["entities"].append(
                {
                    "entity_id": r["entity_id"],
                    "name": r["name"],
                    "domain": r.get("domain") or r["entity_id"].split(".", 1)[0],
                    "state": r.get("state") or "",
                    "unit": r.get("unit") or "",
                    "hints": self._hints(r["entity_id"], catalog),
                    "enabled": self._exposed(r, catalog),
                }
            )
        devices = [groups[k] for k in order]
        for d in devices:
            d["entities"].sort(key=lambda e: (e["domain"], e["name"].lower()))
        devices.sort(key=lambda d: (d["board"], d["area"].lower(), d["name"].lower()))
        return devices

    def _ids_of(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value] if value else []
        if isinstance(value, list):
            return [str(x) for x in value if x]
        return []

    async def _allow_entity(self, eid: str) -> bool:
        if eid.split(".", 1)[0] not in self._domains():
            return False
        catalog = self._catalog()
        meta = catalog.get(eid)
        if isinstance(meta, dict) and "enabled" in meta:
            return bool(meta["enabled"])
        try:
            rows = await self._all_rows()
        except HaError:
            return True
        row = next((r for r in rows if r["entity_id"] == eid), None)
        if row is None:
            return False
        return self._exposed(row, catalog)

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90))
        return self._session

    # ------------------------------------------------------------ backends
    async def _ha(self, text: str, conv: _Conversation, device_id: str | None = None) -> AssistantResult:
        s = self.settings.get()
        _note("ha_request", text=text, agent=s.ha_agent_id or None, conversation_id=conv.ha_conversation_id, device_id=device_id)
        data = await self.ha.conversation(text, s.ha_agent_id or None, conv.ha_conversation_id, device_id=device_id)
        _note("ha_reply", response=data)
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
        self,
        text: str,
        conv: _Conversation,
        speaker: str | None,
        ha_result: AssistantResult,
        room: str | None = None,
        on_llm: Callable[[float | None], None] | None = None,
    ) -> AssistantResult:
        """HA did not understand: let the LLM try. If it is not reachable, keep HA's answer."""
        keep = len(conv.messages)
        try:
            result = await self._llm(text, conv, speaker, room, on_llm)
        except (HaError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.info("LLM fallback unavailable: %s", err)
            del conv.messages[keep:]
            return ha_result
        if result.backend != "direct":
            result.backend = "ha+openai"
        if not result.speech and not result.actions:
            return ha_result
        return result

    async def _entities_text(self) -> str:
        try:
            rows = await self._all_rows()
        except HaError as err:
            return f"(не удалось получить список: {err})"
        catalog = self._catalog()
        rows = [r for r in rows if self._exposed(r, catalog)]
        exposed = rows
        rows = without_unnamed_twins(rows, lambda eid: self._hints(eid, catalog))
        groups: dict[tuple[str, str, str], list[dict[str, str]]] = {}
        for r in rows:
            key = (r.get("device") or r["name"], r.get("area") or "", r.get("device_id") or r["entity_id"])
            groups.setdefault(key, []).append(r)
        room_hints = self.room_hints()
        lines: list[str] = []
        count = 0
        room: str | None = None
        for key in sorted(groups, key=lambda x: (x[1].lower(), x[0].lower(), x[2])):
            dname, area, _ = key
            if area != room:
                room = area
                also = f" (она же: {room_hints[area]})" if room_hints.get(area) else ""
                lines.append(f"{area or 'Без комнаты'}{also}:")
            lines.append(f"  {dname}:")
            for r in sorted(groups[key], key=lambda x: x["name"].lower()):
                if count >= MAX_PROMPT_ENTITIES:
                    break
                hints = self._hints(r["entity_id"], catalog)
                extra = f" (подсказки: {hints})" if hints else ""
                if r.get("domain") == "media_player":
                    # a command this one cannot do goes to its twin: the model hears both lists as one
                    acts = list(r.get("actions") or [])
                    for twin in twins(r["entity_id"], exposed):
                        tw = next((x for x in exposed if x["entity_id"] == twin), {})
                        acts += [a for a in tw.get("actions") or [] if a not in acts]
                    if acts:
                        extra += f" [умеет: {', '.join(acts)}]"
                lines.append(f"    {r['entity_id']} — {r['name']}{extra}")
                count += 1
            if count >= MAX_PROMPT_ENTITIES:
                break
        return "\n".join(lines) if lines else "(нет доступных устройств — отметьте их на вкладке «Устройства»)"

    def _rules(self) -> str:
        return self.settings.get().llm_system_prompt.strip() or SYSTEM_RULES

    # -------------------------------------------------------------- plugins
    def _plugin_rules(self) -> str:
        return self.plugins.rules() if self.plugins is not None else ""

    def _tools(self) -> list[dict[str, Any]]:
        return TOOLS + (self.plugins.tools() if self.plugins is not None else [])

    def _plugin_answer(self, text: str, conv: _Conversation) -> AssistantResult | None:
        """«Какая погода завтра»: a plugin answers from what it read, without the model."""
        if self.plugins is None:
            return None
        now = time.time()
        found = self.plugins.answer(text, followup=now - conv.last_plugin_at < FOLLOWUP_TTL_S)
        if found is None:
            return None
        pid, speech = found
        conv.last_plugin_at = now
        _note("plugin", plugin=pid, speech=speech)
        return AssistantResult(speech=speech, backend="plugin")

    async def _plugin_trigger(
        self, text: str, conv: _Conversation, on_llm: Callable[[float | None], None] | None
    ) -> AssistantResult | None:
        """«Поищи в интернете ...», «спроси у брата ...»: the key words send the phrase to a plugin."""
        if self.plugins is None:
            return None
        found = self.plugins.trigger(text)
        if found is None and conv.plugin_wait and time.time() - conv.plugin_wait_at < PENDING_TTL_S:
            plugin = self.plugins.plugins.get(conv.plugin_wait)
            if plugin is not None and plugin.enabled and text.strip():
                found = (plugin, text.strip(" ,.!?"))
        conv.plugin_wait = ""
        if found is None:
            return None
        plugin, query = found
        self._stage(plugin.title)
        _note("plugin_call", plugin=plugin.title, query=query)
        said = await plugin.respond(query, on_llm)
        if said.ask:
            conv.plugin_wait, conv.plugin_wait_at = plugin.id, time.time()
        return AssistantResult(speech=said.speech, backend=said.backend, expect_reply=said.ask)

    def _unknown_answer(self) -> str:
        web = self.plugins.plugins.get("web_search") if self.plugins is not None else None
        if web is not None and web.enabled:
            return "Не знаю. Скажите «поищи в интернете» и вопрос — я поищу."
        return "Не знаю ответа на этот вопрос."

    def plugin_text_limit(self) -> int:
        """How much page text fits next to the other prompts in the model's context."""
        return min(8000, self.settings.get().llm_ctx * 3 // 4)

    async def plugin_chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        """One request from a plugin: its own slot, only its own tools, the house is not in it."""
        try:
            return await self._chat(
                messages, max_tokens=700, tool_choice="required", tools=tools, slot=SLOT_PLUGINS, timeout=PLUGIN_TIMEOUT_S
            )
        finally:
            if self._local_llm():
                # a long page may have pushed the house out of the cache: read it back before a command
                self.schedule_prewarm(1.0)

    def _notes_rules(self) -> str:
        return self.settings.get().llm_notes_prompt.strip() or NOTES_RULES

    async def _system(self) -> str:
        parts = [self._rules()]
        if self._plugin_rules():
            parts.append(self._plugin_rules())
        parts += [CATALOG_HEADER, await self._entities_text()]
        extra = self.settings.get().llm_prompt.strip()
        if extra:
            parts.append(extra)
        return "\n".join(parts)

    async def prompt_view(self) -> dict[str, Any]:
        """The prompts as the panel shows them: rules to edit, the rest is assembled."""
        s = self.settings.get()
        full = await self._system()
        return {
            "home": {
                "rules": self._rules(),
                "default": SYSTEM_RULES,
                "custom": bool(s.llm_system_prompt.strip()),
                "catalog": f"{CATALOG_HEADER}\n{await self._entities_text()}",
                "plugins": self._plugin_rules(),
                "chars": len(full),
            },
            "notes": {
                "rules": self._notes_rules(),
                "default": NOTES_RULES,
                "custom": bool(s.llm_notes_prompt.strip()),
                "chars": len(self._notes_rules()),
            },
        }

    def _note_prompt(self, name: str, text: str) -> None:
        """The prompt is long and nearly always the same: the log says so, or shows the change."""
        last = self._last_prompts.get(name)
        self._last_prompts[name] = text
        data: dict[str, Any] = {"name": PROMPT_NAMES.get(name, name), "chars": len(text)}
        if last is None:
            data["first"] = True
        elif last != text:
            changed = [ln for ln in ndiff(last.splitlines(), text.splitlines()) if ln[:1] in "+-"]
            data["changes"] = changed[:MAX_DIFF_LINES]
        _note("system", **data)

    def _local_llm(self) -> bool:
        return self.settings.get().llm_url.rstrip("/").startswith("http://127.0.0.1:10360")

    async def prewarm(self) -> None:
        """Read the system prompt into the local model's cache before the first command."""
        if not self._local_llm():
            return
        t0 = time.monotonic()
        try:
            system = await self._system()
            await self._chat([{"role": "system", "content": system}, {"role": "user", "content": "привет"}], max_tokens=1)
            self._cached_system = system
            _LOGGER.info("LLM prompt cached in %.1f s", time.monotonic() - t0)
            if self.settings.get().notes_enabled:
                t0 = time.monotonic()
                rules = self._notes_rules()
                await self._chat(
                    [{"role": "system", "content": rules}, {"role": "user", "content": "привет"}],
                    max_tokens=1, tools=NOTES_TOOLS, slot=SLOT_NOTES,
                )
                self._cached_notes = rules
                _LOGGER.info("LLM notes prompt cached in %.1f s", time.monotonic() - t0)
        except Exception as err:
            _LOGGER.info("LLM prewarm failed: %s", err)

    def schedule_prewarm(self, delay_s: float = 3.0) -> None:
        """Once the changes stop: every tick in the device list would read the whole prompt again."""
        if self._prewarm_task is not None:
            self._prewarm_task.cancel()

        async def later() -> None:
            await asyncio.sleep(delay_s)
            await self.prewarm()

        self._prewarm_task = asyncio.create_task(later())

    def _predict_llm_s(self, system: str) -> float | None:
        """How long the LLM will take: the usual time of the last answers. The number
        of tool steps is the model's own choice, so this is an estimate at best."""
        if self._local_llm() and system != self._cached_system:
            return float("inf")  # the whole device list has to be read first
        if not self.llm_durations:
            return None
        return sorted(self.llm_durations)[len(self.llm_durations) // 2]

    def _reads_as_ids(self, speech: str) -> bool:
        domains = "|".join(re.escape(d) for d in self._domains())
        return bool(re.search(rf"\b(?:{domains})\.[a-z0-9_]+\b|\b(?:{'|'.join(_TOOL_NAMES)})\b", speech))

    def _named_ids(self, text: str) -> list[str]:
        domains = "|".join(re.escape(d) for d in self._domains())
        return list(dict.fromkeys(re.findall(rf"\b(?:{domains})\.[a-z0-9_]+\b", text)))

    async def _recover_calls(self, messages: list[dict[str, Any]], content: str) -> list[dict[str, Any]]:
        """The model wrote entity ids instead of calling a function, in whatever form:
        one id is read as it is; of several, it picks in a request that allows only
        a real call; if even that fails, the ones it named are read."""
        ids = self._named_ids(content)
        if len(ids) != 1:
            try:
                msg = await self._chat(messages, tool_choice="required")
            except (HaError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                _LOGGER.info("LLM retry with a required call failed: %s", err)
            else:
                calls = msg.get("tool_calls") or text_tool_calls((msg.get("content") or "").strip())
                if calls:
                    return calls
        return [
            {
                "id": f"text_{i}",
                "type": "function",
                "function": {"name": "get_state", "arguments": json.dumps({"entity_id": eid})},
            }
            for i, eid in enumerate(ids[:MAX_RECOVERED_READS])
        ]

    @staticmethod
    def _speak_reads(reads: list[dict[str, Any]]) -> str:
        """The values themselves, when the model cannot put them into words."""
        parts = []
        for out in reads[:MAX_SPOKEN_READS]:
            attrs = out.get("attributes") or {}
            value = f"{out.get('state')} {attrs.get('unit_of_measurement') or ''}".strip()
            parts.append(f"{attrs.get('friendly_name') or 'Значение'}: {value}")
        return ". ".join(parts) + "." if parts else ""

    async def _outside_read(self, measure: str, rows: list[dict[str, Any]], catalog: dict[str, Any]) -> tuple[str, str] | None:
        """«Температура на улице» with no such sensor: the weather entity knows. (value, unit)"""
        attr, unit_attr = WEATHER_ATTRS.get(measure, ("", ""))
        weather = [r for r in rows if r.get("domain") == "weather"]
        if not attr or not weather:
            return None
        weather.sort(key=lambda r: "улиц" not in self._hints(r["entity_id"], catalog).lower())
        attrs = (await self.ha.state(weather[0]["entity_id"])).get("attributes") or {}
        if attrs.get(attr) is None:
            return None
        unit = str(attrs.get(unit_attr) or "") if unit_attr else "%"
        _note("direct", measure=measure, chosen=[weather[0]["entity_id"]], attribute=attr)
        return str(attrs[attr]), unit

    async def _direct_read(self, text: str, conv: _Conversation, room: str | None) -> AssistantResult | None:
        """A question about a sensor's value: answered from HA itself, see sensor_lookup."""
        # «Какая погода в зале? Какая температура в зале?»: a person corrects themselves,
        # the last sentence about a measure is the question
        sentences = [x for x in re.split(r"(?<=[.?!])\s+", text.strip()) if asked_measure(x)]
        if len(sentences) > 0 and sentences[-1] != text.strip():
            text = sentences[-1]
        try:
            catalog = self._catalog()
            rows = [r for r in await self._all_rows() if self._exposed(r, catalog)]
            hints = {r["entity_id"]: self._hints(r["entity_id"], catalog) for r in rows}
            now = time.time()
            followup = not asked_measure(text) and bool(conv.last_measure) and now - conv.last_measure_at < FOLLOWUP_TTL_S
            asked = f"{conv.last_measure} {text}" if followup else text
            found = find_sensors(asked, rows, hints, self.room_hints(), room, followup=followup)
            measure = found.measure if found is not None else asked_measure(asked)
            if measure and asks_outside(asked) and (found is None or not found.rows):
                outside = await self._outside_read(measure, rows, catalog)
                if outside is not None:
                    speech = f"На улице, {measure} {spoken_value(outside[0], outside[1], measure)}."
                    conv.last_measure, conv.last_measure_at = measure, now
                    return AssistantResult(speech=speech, backend="direct")
            if found is None:
                return None
            parts = []
            for row in found.rows[:MAX_ANSWERS]:
                state = await self.ha.state(row["entity_id"])
                attrs = state.get("attributes") or {}
                value = state.get("state")
                said = "нет данных" if value in ("unavailable", "unknown", None) else spoken_value(
                    value, attrs.get("unit_of_measurement") or row.get("unit") or "", found.measure
                )
                place = row.get("area") or row.get("device") or row["name"]
                if len(found.rows) > 1:
                    place = f"{place}, {row.get('device') or row['name']}"
                elif found.where:
                    place = found.where.capitalize()  # the way it was asked: «в спальне», not «Кабинет»
                parts.append(f"{place}, {found.measure} {said}")
        except HaError as err:
            _LOGGER.info("Sensor lookup failed, asking the LLM: %s", err)
            return None
        speech = ". ".join(parts) + "." if parts else f"Датчика {found.missing} нет."
        _note(
            "direct", measure=found.measure, scores=found.scores, chosen=[r["entity_id"] for r in found.rows],
            missing=found.missing, speech=speech,
        )
        # not in the model's history: it copies old numbers from there instead of reading
        conv.last_measure, conv.last_measure_at = found.measure, time.time()
        return AssistantResult(speech=speech, backend="direct")

    # ------------------------------------------------- lists, notes, journal
    def _notes_result(
        self, reply: Reply, conv: _Conversation, backend: str = "notes", speaker_id: str | None = None
    ) -> AssistantResult:
        pending = reply.pending
        if pending and pending.get("op") == "list_more" and speaker_id and not pending.get("speaker_id"):
            pending["speaker_id"] = speaker_id
        conv.pending = pending
        conv.pending_until = time.time() + PENDING_TTL_S if pending else 0.0
        hold_s = None
        if pending and pending.get("op") == "list_more":
            hold_s = float(HOLD_OPEN_S if pending.get("open") else HOLD_SETTLE_S)
        _note("notes", speech=reply.speech, pending=pending, actions=reply.actions)
        return AssistantResult(
            speech=reply.speech,
            expect_reply=reply.expect_reply,
            backend=backend,
            actions=reply.actions,
            hold_s=hold_s,
            extend=reply.extend,
        )

    def _voices_known(self) -> bool:
        hub = self.hub
        return bool(hub is not None and hub.runtime.store.has_speakers())

    async def finish_list(
        self, conv_key: str, on_saving: Callable[[str], None] | None = None, ask_more: bool = False
    ) -> AssistantResult | None:
        """The open list was left in silence: check and write it, or ask what to add.
        ask_more: the pause ran out (not a stop): write and ask «Ещё что-нибудь?»."""
        conv = self._convs.get(conv_key)
        pending = conv.pending if conv is not None else None
        if conv is None or not pending or pending.get("op") != "list_more":
            return None
        conv.pending = None
        conv.pending_until = 0.0
        trace = _start_trace()
        said = list(pending.get("items") or [])
        _note("input", text=f"(пауза — пишу список: {', '.join(said) or 'пусто'})")
        items = await self._review_list(pending, said, on_saving)
        reply = _say_dropped(self.notes.save_more(pending, items, ask_more=ask_more), said, items)
        result = self._notes_result(reply, conv)
        result.trace = trace
        return result

    async def _review_list(
        self, pending: dict[str, Any], items: list[str], on_saving: Callable[[str], None] | None
    ) -> list[str]:
        """Say «Записываю», let the model drop what is not an item, keep the rest.
        Without a model, or when it fails, everything said is written as before."""
        if not items or not self._llm_usable():
            return items
        if on_saving is not None:
            on_saving(SAVING_PHRASE)
        self._stage("LLM проверяет список перед записью")
        numbered = "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
        messages = [
            {"role": "system", "content": LIST_REVIEW_RULES},
            {"role": "user", "content": f"List: {self.notes.list_title(pending)}\n{numbered}"},
        ]
        self._note_prompt("list_review", LIST_REVIEW_RULES)
        try:
            msg = await self._chat(messages, max_tokens=80, tools=LIST_REVIEW_TOOLS, slot=SLOT_PLUGINS)
        except (HaError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.info("List review unavailable, writing as said: %s", err)
            return items
        calls = msg.get("tool_calls") or []
        if not calls:
            found = _json_tool_calls((msg.get("content") or "").strip())
            calls = [{"function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for n, a in found]
        args: Any = None
        for call in calls:
            fn = call.get("function") or {}
            if fn.get("name") == "keep_items":
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = None
                break
        keep = args.get("keep") if isinstance(args, dict) else None
        if not isinstance(keep, list):
            _note("list_review", items=items, error="no keep_items", kept=items)
            return items
        idx = {int(n) for n in keep if isinstance(n, (int, float)) or (isinstance(n, str) and n.strip().isdigit())}
        kept = [item for i, item in enumerate(items, 1) if i in idx]
        dropped = [item for item in items if item not in kept]
        _note("list_review", items=items, kept=kept, dropped=dropped)
        if dropped:
            _LOGGER.info("List review dropped: %s", dropped)
        return kept

    async def _notes(
        self,
        text: str,
        conv: _Conversation,
        speaker: str | None,
        on_llm: Callable[[float | None], None] | None,
        speaker_id: str | None = None,
        on_saving: Callable[[str], None] | None = None,
    ) -> AssistantResult | None:
        """The first stop of every phrase, and instant. None: not about lists."""
        pending, conv.pending = conv.pending, None
        if pending and time.time() < conv.pending_until:
            if pending.get("op") == "list_more":
                more = self.notes.take_more(text, pending, speaker, speaker_id, self._voices_known())
                if more.save is not None:
                    items = await self._review_list(pending, more.save, on_saving)
                    more.reply = self.notes.save_more(pending, items, handoff=more.kind == "handoff")
                    if more.kind != "handoff":
                        more.reply = _say_dropped(more.reply, more.save, items)
                if more.kind == "handoff":
                    conv.list_prefix = more.reply.speech
                else:
                    return self._notes_result(more.reply, conv, speaker_id=speaker_id)
            elif pending.get("op") == "again":
                # the model asked: it hears its own command again, now with the answer
                return await self._notes_llm(f"{pending.get('text', '')}. {text}", conv, speaker, on_llm)
            else:
                reply = self.notes.answer(text, pending, speaker)
                if reply is not None:
                    reply, used = await self._match(reply, speaker, on_llm)
                    return self._notes_result(reply, conv, "notes+llm" if used else "notes", speaker_id=speaker_id)
        reply = self.notes.handle(text, speaker)
        if reply is not None:
            reply, used = await self._match(reply, speaker, on_llm)
            return self._notes_result(reply, conv, "notes+llm" if used else "notes", speaker_id=speaker_id)
        if in_domain(text) and self._llm_usable():
            return await self._notes_llm(text, conv, speaker, on_llm)
        return None

    async def _match(
        self, reply: Reply, speaker: str | None, on_llm: Callable[[float | None], None] | None
    ) -> tuple[Reply, bool]:
        """The code did not find a list or an item: one short call to the model, which
        matches by meaning. Without a model, or when it finds nothing, the reply stands.
        Twice at most: a list found by meaning may then miss an item too."""
        used = False
        for _ in range(2):
            match, reply.match = reply.match, None
            if not match or not self._llm_usable():
                break
            question = self.notes.match_question(match)
            if question is None:
                break
            picks = await self._llm_match(*question, on_llm)
            done = self.notes.finish_match(match, picks, speaker) if picks is not None else None
            if done is None:
                break
            reply, used = done, True
        reply.match = None
        return reply, used

    async def _llm_match(
        self, options: list[str], said: list[str], on_llm: Callable[[float | None], None] | None
    ) -> list[int] | None:
        if not options or not said:
            return None
        self._stage("LLM ищет похожее в списках")
        if on_llm is not None:
            on_llm(None)
        listing = "\n".join(f"{i}. {o}" for i, o in enumerate(options, 1))
        heard = "\n".join(f"{i}. {s}" for i, s in enumerate(said, 1))
        messages = [
            {"role": "system", "content": LIST_MATCH_RULES},
            {"role": "user", "content": f"Options:\n{listing}\n\nSaid:\n{heard}"},
        ]
        self._note_prompt("list_match", LIST_MATCH_RULES)
        try:
            msg = await self._chat(messages, max_tokens=40, tools=LIST_MATCH_TOOLS, slot=SLOT_PLUGINS)
        except (HaError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.info("List match unavailable: %s", err)
            return None
        calls = msg.get("tool_calls") or []
        if not calls:
            found = _json_tool_calls((msg.get("content") or "").strip())
            calls = [{"function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for n, a in found]
        for call in calls:
            fn = call.get("function") or {}
            if fn.get("name") != "match":
                continue
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                break
            raw = args.get("picks") if isinstance(args, dict) else None
            if not isinstance(raw, list):
                break
            picks = [int(n) if isinstance(n, (int, float)) or (isinstance(n, str) and n.strip().isdigit()) else 0 for n in raw]
            picks = (picks + [0] * len(said))[: len(said)]
            _note("list_match", options=options, said=said, picks=picks)
            return picks
        _note("list_match", options=options, said=said, error="no match call")
        return None

    def _llm_usable(self) -> bool:
        s = self.settings.get()
        return bool(s.llm_url) and (s.assistant_backend == "openai" or s.llm_fallback)

    async def _notes_llm(
        self, text: str, conv: _Conversation, speaker: str | None, on_llm: Callable[[float | None], None] | None
    ) -> AssistantResult | None:
        """About lists, but in words the code cannot read: a short prompt and one call.
        The model only says what to do; the same code does it and words the answer."""
        rules = self._notes_rules()
        self._note_prompt("notes", rules)
        self._stage("LLM разбирает команду про списки")
        if on_llm is not None:
            cached = not self._local_llm() or rules == self._cached_notes
            on_llm(None if cached else float("inf"))
        messages = [
            {"role": "system", "content": rules},
            {"role": "user", "content": f"[{self.notes.names_line()}]\n{text}"},
        ]
        try:
            msg = await self._chat(messages, max_tokens=120, tools=NOTES_TOOLS, slot=SLOT_NOTES)
        except (HaError, aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
            _LOGGER.info("LLM for notes unavailable: %s", err)
            return None
        self._cached_notes = rules
        calls = msg.get("tool_calls") or []
        if not calls:
            found = _json_tool_calls((msg.get("content") or "").strip())
            calls = [{"function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}} for n, a in found]
        if not calls:
            return self._notes_result(
                Reply("Не поняла, что сделать со списком. Скажите, например: добавь в список покупок молоко."), conv
            )
        fn = calls[0].get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        _note("tool", name=fn.get("name"), args=args)
        reply = self.notes.run_tool(fn.get("name", ""), args, speaker)
        reply, _ = await self._match(reply, speaker, on_llm)
        if reply.pending and reply.pending.get("op") == "again":
            reply.pending["text"] = text
        return self._notes_result(reply, conv, "notes+llm")

    async def _llm(
        self,
        text: str,
        conv: _Conversation,
        speaker: str | None,
        room: str | None = None,
        on_llm: Callable[[float | None], None] | None = None,
    ) -> AssistantResult:
        if self.settings.get().direct_reads:
            direct = await self._direct_read(text, conv, room)
            if direct is not None:
                return direct
        system = await self._system()
        self._note_prompt("home", system)
        if on_llm is not None:
            on_llm(self._predict_llm_s(system))
        t0 = time.monotonic()
        result = await self._llm_rounds(text, conv, speaker, room, system)
        self._cached_system = system
        self.llm_durations.append(time.monotonic() - t0)
        return result

    async def _llm_rounds(
        self, text: str, conv: _Conversation, speaker: str | None, room: str | None, system: str
    ) -> AssistantResult:
        keep = len(conv.messages)
        conv.messages.append(_user_message(text, speaker, room, conv.messages))
        result = AssistantResult(backend="openai")
        tools_ran = False
        nudged = False
        echoed = False
        reads: list[dict[str, Any]] = []
        # the promise and the nudge, for one request only: kept out of the history
        extra: list[dict[str, Any]] = []
        # the memory goes with this request only: the history keeps the phrase as it was said
        memo = self._memory_block(conv, keep)
        for step in range(MAX_TOOL_ROUNDS):
            self._stage("LLM думает" if step == 0 else f"LLM думает (шаг {step + 1})")
            talk = list(conv.messages)
            if memo:
                said, content = talk[keep].get("_text", ""), talk[keep]["content"]
                # after the context line «[time: ...]», right before the phrase
                head = content[: len(content) - len(said)] if said and content.endswith(said) else ""
                talk[keep] = {**talk[keep], "content": head + memo + content[len(head):]}
            msg = await self._chat([{"role": "system", "content": system}, *talk, *extra])
            extra = []
            calls = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()
            if not calls:
                calls = text_tool_calls(content)
                if calls:
                    # stored as a real call: copied from the history, the text form would come back
                    _LOGGER.info("LLM wrote a tool call as text, executing it: %r", content)
                    msg = {"role": "assistant", "content": "", "tool_calls": calls}
            question = _asked_back(calls, content)
            if question and is_echo(question, text):
                _LOGGER.info("LLM asked the user's own question back: %r", question)
                if not echoed:
                    echoed = True
                    extra = [{"role": "assistant", "content": question}, {"role": "user", "content": ECHO_NUDGE}]
                    continue
                del conv.messages[keep:]
                result.speech = self._unknown_answer()
                result.error = f"Модель повторила вопрос вместо ответа: {question[:200]}"
                return result
            # a number nobody has read: the model made it up («22 градусов»)
            invented = not calls and not tools_ran and (
                (bool(re.search(r"\d", content)) and bool(asked_measure(text))) or bool(_SPOKEN_VALUE_RE.search(content))
            )
            if invented or (not calls and self._reads_as_ids(content)):
                _LOGGER.info("LLM wrote service names or an unread value instead of an answer: %r", content)
                if not tools_ran:
                    calls = await self._recover_calls([{"role": "system", "content": system}, *conv.messages], content)
                    if calls:
                        msg = {"role": "assistant", "content": "", "tool_calls": calls}
                if not calls:
                    # nothing to run: this exchange stays out of the history
                    speech = self._speak_reads(reads) or ("Готово." if result.actions else "")
                    if speech:
                        conv.messages.append({"role": "assistant", "content": speech})
                        result.speech = speech
                        return result
                    del conv.messages[keep:]
                    result.speech = "Не получилось разобраться с запросом."
                    result.error = f"Модель ответила служебным текстом: {content[:200]}"
                    return result
            promised = is_promise(content)
            if not calls and not tools_ran and not nudged and (promised or is_refusal(content) or is_claim(content)):
                _LOGGER.info("LLM answered without calling a tool, asking again: %r", content)
                nudged = True
                nudge = PROMISE_NUDGE if promised else UNBACKED_NUDGE
                extra = [{"role": "assistant", "content": content}, {"role": "user", "content": nudge}]
                continue
            if not calls and not tools_ran and nudged and is_promise(content):
                # a second promise: an answer that never comes is worse than "no"
                _LOGGER.warning("LLM promised again without a tool call: %r", content)
                result.speech = "Не получилось это узнать."
                result.error = f"Модель пообещала, но не вызвала функцию: {content[:200]}"
                return result
            if not calls and not tools_ran and nudged and is_claim(content):
                _LOGGER.warning("LLM claimed an action again without a tool call: %r", content)
                del conv.messages[keep:]
                result.speech = "Не получилось выполнить команду."
                result.error = f"Модель сказала, что сделала, но не вызвала функцию: {content[:200]}"
                return result
            conv.messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
            if not calls:
                result.speech = content
                result.expect_reply = result.speech.endswith("?")
                if is_refusal(content) and not result.actions and not reads:
                    del conv.messages[keep:]
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
                tools_ran = True
                output = await self._tool(name, args, result)
                _note("tool", name=name, args=args, output=output)
                if name == "get_state" and isinstance(output, dict) and "state" in output:
                    reads.append(output)
                conv.messages.append(
                    {"role": "tool", "tool_call_id": call.get("id", ""), "content": json.dumps(output, ensure_ascii=False)}
                )
        result.speech = "Готово." if result.actions else "Не получилось разобраться с запросом."
        return result

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        max_tokens: int = 300,
        tool_choice: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        slot: int = SLOT_HOME,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        s = self.settings.get()
        headers = {"Content-Type": "application/json"}
        if s.llm_api_key:
            headers["Authorization"] = f"Bearer {s.llm_api_key}"
        messages = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
        body: dict[str, Any] = {
            "model": s.llm_model, "messages": messages, "tools": tools or self._tools(), "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if tool_choice:
            body["tool_choice"] = tool_choice
        if self._local_llm():
            body["cache_prompt"] = True
            body["id_slot"] = slot
        session = await self._http()
        _note(
            "llm_request", model=s.llm_model, tool_choice=tool_choice,
            messages=[m for m in messages if m.get("role") != "system"],
        )
        t0 = time.monotonic()
        extra: dict[str, Any] = {"timeout": aiohttp.ClientTimeout(total=timeout)} if timeout else {}
        async with session.post(s.llm_url.rstrip("/") + "/chat/completions", json=body, headers=headers, **extra) as resp:
            text = await resp.text()
            if resp.status >= 400:
                _note("llm_reply", status=resp.status, body=text[:2000], seconds=round(time.monotonic() - t0, 1))
                raise HaError(f"LLM {resp.status}: {text[:200]}")
            data = json.loads(text)
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        _note("llm_reply", message=msg, finish_reason=choice.get("finish_reason"), usage=data.get("usage"), seconds=round(time.monotonic() - t0, 1))
        return msg

    async def _able_players(self, service: str, targets: list[str]) -> list[str] | str:
        """Each player, or its twin if only the twin can do this. A str: the one nobody can stand in for."""
        able: list[str] = []
        for eid in targets:
            if supports(service, await self.ha.state(eid)):
                able.append(eid)
                continue
            catalog = self._catalog()
            rows = [r for r in await self._all_rows() if self._exposed(r, catalog)]
            for twin in twins(eid, rows):
                if supports(service, await self.ha.state(twin)):
                    able.append(twin)
                    break
            else:
                return eid
        return list(dict.fromkeys(able))

    async def _tool(self, name: str, args: dict[str, Any], result: AssistantResult) -> Any:
        plugin = self.plugins.owner(name) if self.plugins is not None else None
        if plugin is not None:
            return await plugin.call_tool(name, args)
        domains = self._domains()
        try:
            if name == "list_entities":
                catalog = self._catalog()
                dom = str(args.get("domain") or "").strip()
                area = str(args.get("area") or "").strip().lower()
                rows = await self.ha.entities([dom] if dom in domains else domains)
                rows = [r for r in rows if self._exposed(r, catalog)]
                if area:
                    room_hints = self.room_hints()
                    rows = [
                        r for r in rows
                        if area in (r["area"] or "").lower()
                        or area in (r.get("device") or "").lower()
                        or area in room_hints.get(r["area"] or "", "").lower()
                    ]
                out = []
                for r in rows[:100]:
                    item = {
                        "entity_id": r["entity_id"],
                        "name": r["name"],
                        "state": r.get("state"),
                        "area": r.get("area") or "",
                        "device": r.get("device") or "",
                    }
                    hints = self._hints(r["entity_id"], catalog)
                    if hints:
                        item["hints"] = hints
                    out.append(item)
                return out
            if name == "get_state":
                eid = str(args.get("entity_id") or "")
                if not await self._allow_entity(eid):
                    # usually an id the model made up: it gets real ones to try again
                    catalog = self._catalog()
                    known = [r["entity_id"] for r in await self._all_rows() if self._exposed(r, catalog)]
                    return {
                        "error": f"entity_id {eid} нет в справочнике, вызови get_state с точным entity_id из него",
                        "похожие": get_close_matches(eid, known, n=5, cutoff=0.3),
                    }
                st = await self.ha.state(eid)
                attrs = {k: v for k, v in (st.get("attributes") or {}).items() if not isinstance(v, (list, dict))}
                return {"entity_id": eid, "state": st.get("state"), "attributes": attrs}
            if name == "call_service":
                domain = str(args.get("domain") or "")
                service = str(args.get("service") or "")
                if domain not in ACTION_DOMAINS or domain not in domains:
                    return {"error": f"управление доменом {domain} не разрешено"}
                targets = self._ids_of(args.get("entity_id"))
                for eid in targets:
                    if not await self._allow_entity(eid):
                        return {"error": f"{eid} недоступна ассистенту"}
                data = dict(args.get("data") or {})
                target = args.get("entity_id")
                if domain == "media_player":
                    service = service_name(service)
                    try:
                        data = service_data(service, data)
                    except ValueError as err:
                        return {"error": str(err)}
                    able = await self._able_players(service, targets)
                    if isinstance(able, str):
                        return {"error": f"{able} не умеет {service}"}
                    target = able[0] if len(able) == 1 else able
                data["entity_id"] = target
                await self.ha.call_service(domain, service, data)
                result.actions.append({"service": f"{domain}.{service}", "target": target, "data": data if domain == "media_player" else args.get("data")})
                return {"ok": True}
        except HaError as err:
            return {"error": str(err)}
        return {"error": f"неизвестная функция {name}"}

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()


def _context_line(ctx: dict[str, str]) -> str:
    parts = [f"{k}: {ctx[k]}" for k in ("time", "speaker", "room") if k in ctx]
    return f"[{'; '.join(parts)}]\n" if parts else ""


def _known_context(messages: list[dict[str, Any]]) -> tuple[dict[str, str], float]:
    """What the model already knows from the history: the latest of each field, and when the time was sent."""
    known: dict[str, str] = {}
    time_at = 0.0
    for m in messages:
        ctx = m.get("_ctx") or {}
        known.update(ctx)
        if "time" in ctx:
            time_at = m.get("_at", 0.0)
    return known, time_at


def _user_message(text: str, speaker: str | None, room: str | None, history: list[dict[str, Any]]) -> dict[str, Any]:
    """The phrase with only the context the history does not already carry, in English."""
    now = datetime.now()
    # a voice not recognised mid-talk is nearly always the same person: nothing is sent then
    current = {"speaker": speaker or "", "room": room or ""}
    known, time_at = _known_context(history)
    ctx = {k: v for k, v in current.items() if v and known.get(k) != v}
    if "time" not in known or time.time() - time_at > CONTEXT_TIME_RESEND_S:
        ctx = {"time": f"{_WEEKDAYS[now.weekday()]} {now:%Y-%m-%d %H:%M}", **ctx}
    return {
        "role": "user", "content": _context_line(ctx) + text,
        "_text": text, "_ctx": ctx, "_at": time.time(),
    }


def _trim_history(messages: list[dict[str, Any]], keep: int = 12) -> list[dict[str, Any]]:
    """The last messages, starting at a phrase: a tool answer without its call confuses
    the model. Context sent in the dropped messages moves to the first phrase kept."""
    tail = messages[-keep:]
    while tail and tail[0].get("role") != "user":
        tail.pop(0)
    if tail and len(tail) < len(messages) and "_text" in tail[0]:
        known, time_at = _known_context(messages[: len(messages) - len(tail) + 1])
        tail[0] = {**tail[0], "content": _context_line(known) + tail[0]["_text"], "_ctx": known, "_at": time_at}
    return tail


def result_dict(result: AssistantResult) -> dict[str, Any]:
    return asdict(result)

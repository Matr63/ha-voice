"""Lists, notes and journals kept by the assistant.

«Добавь в список покупок на дачу молоко», «что в списке для дачи», «какие есть
списки», «запиши заметку», «запиши в журнал»: an answer has to come at once and
a small model mixes up names, so ordinary phrases are understood by code here.
A question back («Что добавить?») is a pending step answered by the next phrase.
Only the phrases the code cannot read go to the model, with a prompt of their own
(see NOTES_RULES); what it asks for is carried out by the same functions.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sensor_lookup import _same_word, asked_measure

_LOGGER = logging.getLogger("ha-voice.notes")

KINDS = ("list", "notes", "journal")
MAX_WORDS = 60
MAX_TEXT = 500
READ_ITEMS = 10
READ_ENTRIES = 5

_PUNCT = ".,!?;:«»\"'“”„()…—–"
_FILLERS = {"а", "ну", "и", "так", "давай", "пожалуйста", "ещё", "еще", "теперь", "мне", "нам", "слушай", "скажи-ка"}
_PREPS = {"на", "для", "в", "во", "к", "ко", "по", "у", "о", "об", "про", "с", "со", "за", "до", "от", "из"}
_NAME_STOP = {
    "список", "списка", "списку", "списке", "списком", "пожалуйста", "мой", "моём", "моем", "мою", "моего", "наш",
    "нашем", "нашу", "нашего", "есть", "там", "тут", "под", "названием", "называется", "который", "этот", "этом", "этого",
}
_LIST_WORDS = {"список", "списка", "списку", "списке", "списком", "списки", "списков", "списках", "спискам"}
_LIST_PLURAL = {"списки", "списков", "списках", "спискам"}
_BUY_WORDS = {"покупки", "покупок", "покупках", "покупкам", "покупку"}
_ADD = ("добав", "допиш", "дописа", "внес", "внёс", "занес", "занеси", "запиш", "запис", "закин", "кинь", "впиш", "дополн", "сохран")
_NOTE_VERBS = ("запиш", "запис", "запомн", "сохран")
_READ = ("прочит", "прочт", "зачит", "покаж", "скаж", "открой", "озвуч", "перечисл", "назов", "напомни", "проверь", "посмотр")
_ASK = {"что", "чего", "какие", "какой", "какая", "какую", "сколько"}
_REMOVE = ("удал", "убер", "убра", "вычеркн", "сотр", "выкин", "выброс", "исключ")
_CLEAR = ("очист",)
_CREATE = ("созда", "создай", "завед", "заведи", "начни", "начать", "сделай", "сделать", "новый", "новая", "новую")
_LAST = ("последн",)
_YES = {"да", "давай", "ага", "угу", "конечно", "можно", "создай", "создавай", "добавь", "хорошо", "верно", "туда", "удаляй", "удали", "очисти", "новый"}
_NO = {"нет", "не", "неа", "отмена", "отмени", "отбой", "забудь", "неважно", "ничего", "никакой", "стоп"}
_DOMAIN_STEMS = ("спис", "заметк", "заметок", "журнал", "покупк", "покупок", "запиш", "запомн", "купить")
_DEVICE_VERBS = ("включ", "выключ")
_ITEM_SPLIT = re.compile(r"\s*,\s*|\s+и\s+|\s+а\s+ещ[её]\s+|\s+плюс\s+", re.IGNORECASE)
_ORDINALS = (
    "первое", "второе", "третье", "четвёртое", "пятое", "шестое", "седьмое", "восьмое", "девятое", "десятое",
    "одиннадцатое", "двенадцатое", "тринадцатое", "четырнадцатое", "пятнадцатое", "шестнадцатое", "семнадцатое",
    "восемнадцатое", "девятнадцатое", "двадцатое", "двадцать первое", "двадцать второе", "двадцать третье",
    "двадцать четвёртое", "двадцать пятое", "двадцать шестое", "двадцать седьмое", "двадцать восьмое",
    "двадцать девятое", "тридцатое", "тридцать первое",
)
_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

NOTES_RULES = """Ты разбираешь голосовые команды про списки, заметки и журнал. Всегда вызывай ровно одну функцию и ничего не пиши текстом.
- Список — покупки, дела и всё, что перечисляют по пунктам. Заметка — то, что просят запомнить. Журнал — записи о том, что было сделано.
- Название списка бери из перечня «Есть» в начале сообщения, если подходит по смыслу. Иначе возьми слова из фразы.
- Если неясно, что сделать, вызови ask_user с коротким вопросом."""


def _tool(name: str, description: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


_STR = {"type": "string"}
_ITEMS = {"type": "array", "items": {"type": "string"}}
NOTES_TOOLS = [
    _tool("list_add", "Добавить пункты в список.", {"list": _STR, "items": _ITEMS}, ["list", "items"]),
    _tool("list_read", "Прочитать список.", {"list": _STR}, ["list"]),
    _tool("list_remove", "Убрать пункты из списка.", {"list": _STR, "items": _ITEMS}, ["list", "items"]),
    _tool("lists_overview", "Назвать, какие есть списки.", {}, []),
    _tool("note_add", "Записать заметку.", {"text": _STR}, ["text"]),
    _tool("notes_read", "Прочитать заметки.", {}, []),
    _tool("journal_add", "Сделать запись в журнале.", {"text": _STR}, ["text"]),
    _tool("journal_read", "Прочитать журнал.", {}, []),
    _tool("ask_user", "Задать уточняющий вопрос.", {"question": _STR}, ["question"]),
]


@dataclass
class Reply:
    speech: str
    expect_reply: bool = False
    pending: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)


def _low(word: str) -> str:
    return word.strip(_PUNCT).lower().replace("ё", "е")


def _tokens(text: str) -> list[str]:
    """Words with their commas, without quotes: «на дачу» and на дачу are one name."""
    return [w for w in re.sub(r"[«»\"“”„]", " ", text or "").split() if w.strip(_PUNCT)]


def _join(words: list[str]) -> str:
    return " ".join(words).strip(_PUNCT + " ")


def _content(words: list[str]) -> list[str]:
    low = [_low(w) for w in words]
    return [w for w in low if w and w not in _PREPS and w not in _NAME_STOP and w not in _LIST_WORDS]


def _lower_first(item: str) -> str:
    """«Молоко» only because the recogniser starts a sentence so; an abbreviation stays."""
    return item[0].lower() + item[1:] if len(item) > 1 and item[1].islower() else item


def split_items(text: str) -> list[str]:
    return [_lower_first(p) for p in (x.strip(_PUNCT + " ") for x in _ITEM_SPLIT.split(text or "")) if p]


def in_domain(text: str) -> bool:
    """The phrase is about lists or notes, even if the code could not read it."""
    return any(_low(w).startswith(_DOMAIN_STEMS) for w in _tokens(text))


def _word_in(word: str, words: list[str]) -> bool:
    return any(_same_word(word, w) for w in words)


class NotesBook:
    """The store and everything that can be asked of it. Runs on the event loop."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.items: list[dict[str, Any]] = []
        try:
            if self.path.is_file():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.items = [c for c in data.get("collections", []) if isinstance(c, dict)]
        except (OSError, ValueError):
            _LOGGER.exception("Failed to read %s", self.path)

    # ---------------------------------------------------------------- store
    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"collections": self.items}, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            _LOGGER.exception("Failed to save %s", self.path)

    def all(self) -> list[dict[str, Any]]:
        return sorted(self.items, key=lambda c: (KINDS.index(c["kind"]) if c["kind"] in KINDS else 9, c["name"].lower()))

    def get(self, cid: str) -> dict[str, Any] | None:
        return next((c for c in self.items if c["id"] == cid), None)

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [c for c in self.items if c["kind"] == kind]

    def create(self, name: str, kind: str = "list") -> dict[str, Any]:
        if kind not in KINDS:
            raise ValueError("Неизвестный вид записи")
        name = str(name or "").strip(_PUNCT + " ")[:80]
        if kind == "list" and not name:
            raise ValueError("Нужно название списка")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        coll = {"id": uuid.uuid4().hex[:12], "name": name, "kind": kind, "created": now, "updated": now, "items": []}
        self.items.append(coll)
        self._save()
        return coll

    def rename(self, cid: str, name: str) -> dict[str, Any]:
        coll = self.get(cid)
        if coll is None:
            raise KeyError(cid)
        name = str(name or "").strip(_PUNCT + " ")[:80]
        if coll["kind"] == "list" and not name:
            raise ValueError("Нужно название списка")
        coll["name"] = name
        self._save()
        return coll

    def delete(self, cid: str) -> None:
        if self.get(cid) is None:
            raise KeyError(cid)
        self.items = [c for c in self.items if c["id"] != cid]
        self._save()

    def add_items(self, cid: str, texts: list[str], who: str | None = None) -> tuple[list[str], list[str]]:
        """Returns what was added and what the list already had."""
        coll = self.get(cid)
        if coll is None:
            raise KeyError(cid)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        added: list[str] = []
        known: list[str] = []
        for text in texts:
            text = str(text or "").strip()[:MAX_TEXT]
            if not text:
                continue
            if coll["kind"] == "list" and any(_low(i["text"]) == _low(text) for i in coll["items"]):
                known.append(text)
                continue
            coll["items"].append({"id": uuid.uuid4().hex[:12], "text": text, "ts": now, "who": who})
            added.append(text)
        coll["updated"] = now
        self._save()
        return added, known

    def remove_item(self, cid: str, item_id: str) -> None:
        coll = self.get(cid)
        if coll is None or not any(i["id"] == item_id for i in coll["items"]):
            raise KeyError(item_id)
        coll["items"] = [i for i in coll["items"] if i["id"] != item_id]
        self._save()

    def _clear(self, coll: dict[str, Any]) -> None:
        coll["items"] = []
        self._save()

    # ------------------------------------------------------------ searching
    def find(self, words: list[str], kind: str = "list") -> list[dict[str, Any]]:
        """Every word asked for is in the name, in any case form: an exact name wins."""
        query = _content(words)
        if not query:
            return []
        full = []
        for coll in self.of_kind(kind):
            own = _content(_tokens(coll["name"]))
            if all(_word_in(q, own) for q in query):
                full.append((coll, len(own) == len(query)))
        exact = [c for c, same in full if same]
        return exact or [c for c, _ in full]

    def similar(self, words: list[str], kind: str = "list") -> list[dict[str, Any]]:
        query = _content(words)
        return [
            c for c in self.of_kind(kind)
            if any(_word_in(q, _content(_tokens(c["name"]))) for q in query)
        ]

    def _resolve(self, tail: list[str], kind: str = "list") -> tuple[list[str], list[str]]:
        """Where the name ends and the items start: a known name first, otherwise one
        word with its «на дачу» / «для дома». A known name is not cut inside «для бани»:
        «покупок для бани веник» is another list, not «бани веник» in «покупок на дачу»."""
        if not tail:
            return [], []
        i = 2 if _low(tail[0]) in _PREPS else 1
        while i + 1 < len(tail) and _low(tail[i]) in _PREPS:
            i += 2
        for k in range(len(tail), i - 1, -1):
            if k and _content(tail[:k]) and self.find(tail[:k], kind):
                return tail[:k], tail[k:]
        return tail[:i], tail[i:]

    # ---------------------------------------------------------------- words
    @staticmethod
    def title(coll: dict[str, Any]) -> str:
        if coll["kind"] == "notes":
            return "заметки"
        word = "журнал" if coll["kind"] == "journal" else "список"
        return f"{word} {coll['name']}".strip()

    @staticmethod
    def in_title(coll: dict[str, Any]) -> str:
        if coll["kind"] == "notes":
            return "заметках"
        word = "журнале" if coll["kind"] == "journal" else "списке"
        return f"{word} {coll['name']}".strip()

    @staticmethod
    def _to_title(coll: dict[str, Any]) -> str:
        if coll["kind"] == "notes":
            return "заметки"
        word = "журнал" if coll["kind"] == "journal" else "список"
        return f"{word} {coll['name']}".strip()

    @staticmethod
    def _names(colls: list[dict[str, Any]]) -> str:
        names = [c["name"] or "основной" for c in colls]
        return ", ".join(names[:-1]) + f" или {names[-1]}" if len(names) > 1 else "".join(names)

    @staticmethod
    def _day(ts: str, now: datetime | None = None) -> str:
        now = now or datetime.now().astimezone()
        try:
            day = datetime.fromisoformat(ts).date()
        except (TypeError, ValueError):
            return ""
        if day == now.date():
            return "Сегодня"
        if day == (now - timedelta(days=1)).date():
            return "Вчера"
        return f"{_ORDINALS[day.day - 1]} {_MONTHS[day.month - 1]}".capitalize()

    # -------------------------------------------------------------- actions
    def _ask(self, question: str, **pending: Any) -> Reply:
        return Reply(question, expect_reply=True, pending=pending)

    def _single(self, kind: str) -> dict[str, Any] | None:
        """The notes and the main journal need no name and appear on first use."""
        for coll in self.of_kind(kind):
            if not coll["name"]:
                return coll
        return None

    def _default(self, kind: str) -> dict[str, Any]:
        return self._single(kind) or self.create("", kind)

    def _do_add(self, coll: dict[str, Any], items: list[str], who: str | None) -> Reply:
        added, known = self.add_items(coll["id"], items, who)
        action = [{"service": "записано", "target": f"{self.title(coll)}: {'; '.join(added)}"}] if added else []
        if coll["kind"] == "list":
            parts = []
            if added:
                parts.append(f"Добавлено в {self._to_title(coll)}: {', '.join(added)}.")
            if known:
                parts.append(f"Уже есть: {', '.join(known)}.")
            return Reply(" ".join(parts) or "Нечего добавлять.", actions=action)
        return Reply("Записано в журнал." if coll["kind"] == "journal" else "Записано в заметки.", actions=action)

    def _do_read(self, coll: dict[str, Any]) -> Reply:
        items = coll["items"]
        if not items:
            return Reply(f"{self.title(coll).capitalize()} пока пуст." if coll["kind"] != "notes" else "Заметок пока нет.")
        if coll["kind"] == "list":
            shown = [i["text"] for i in items[:READ_ITEMS]]
            more = f" И ещё {len(items) - READ_ITEMS}." if len(items) > READ_ITEMS else ""
            return Reply(f"В {self.in_title(coll)}: {', '.join(shown)}.{more}")
        newest = list(reversed(items))[:READ_ENTRIES]
        if coll["kind"] == "notes":
            more = f" И ещё {len(items) - READ_ENTRIES}." if len(items) > READ_ENTRIES else ""
            return Reply("Заметки: " + ". ".join(i["text"].rstrip(".") for i in newest) + "." + more)
        lines: list[str] = []
        last = None
        for item in newest:
            day = self._day(item.get("ts", ""))
            text = item["text"].rstrip(".")
            lines.append(f"{day}: {text}" if day and day != last else text)
            last = day
        return Reply(". ".join(lines) + ".")

    def _do_remove(self, coll: dict[str, Any], items: list[str], last: bool = False) -> Reply:
        if last:
            if not coll["items"]:
                return Reply(f"В {self.in_title(coll)} ничего нет.")
            gone = coll["items"][-1]
            self.remove_item(coll["id"], gone["id"])
            return Reply(f"Убрано: {gone['text']}.", actions=[{"service": "убрано", "target": f"{self.title(coll)}: {gone['text']}"}])
        removed: list[str] = []
        missing: list[str] = []
        for want in items:
            query = _content(_tokens(want))
            hit = next((i for i in coll["items"] if query and all(_word_in(q, _content(_tokens(i["text"]))) for q in query)), None)
            if hit is None:
                missing.append(want)
            else:
                self.remove_item(coll["id"], hit["id"])
                removed.append(hit["text"])
        parts = []
        if removed:
            parts.append(f"Убрано: {', '.join(removed)}.")
        if missing:
            parts.append(f"В {self.in_title(coll)} нет: {', '.join(missing)}.")
        action = [{"service": "убрано", "target": f"{self.title(coll)}: {'; '.join(removed)}"}] if removed else []
        return Reply(" ".join(parts) or "Нечего убирать.", actions=action)

    def _overview(self, kind: str) -> Reply:
        colls = [c for c in self.of_kind(kind)]
        if kind == "journal":
            names = [c["name"] or "основной" for c in colls]
            return Reply(f"Журналы: {', '.join(names)}." if names else "Журналов пока нет.")
        if not colls:
            return Reply("Списков пока нет.")
        return Reply(f"Есть списки: {', '.join(c['name'] for c in colls)}.")

    def _on(self, then: str, coll: dict[str, Any], items: list[str], who: str | None, last: bool = False) -> Reply:
        """Do `then` with a collection that is now known."""
        if then == "add":
            if items:
                return self._do_add(coll, items, who)
            return self._ask(f"Что добавить в {self._to_title(coll)}?", op="items", id=coll["id"])
        if then == "read":
            return self._do_read(coll)
        if then == "remove":
            if items or last:
                return self._do_remove(coll, items, last)
            return self._ask("Что убрать?", op="remove_what", id=coll["id"])
        if then == "delete":
            return self._ask(f"Удалить {self._to_title(coll)} целиком?", op="confirm", then="delete", id=coll["id"])
        if then == "clear":
            return self._ask(f"Очистить {self._to_title(coll)}?", op="confirm", then="clear", id=coll["id"])
        return Reply("Не поняла, что сделать.")

    def _act(self, then: str, kind: str, query: list[str], items: list[str], who: str | None, last: bool = False) -> Reply:
        """Finds the collection the words mean, or asks which one."""
        word = "журнала" if kind == "journal" else "списка"
        colls = self.of_kind(kind)
        if not _content(query):
            if len(colls) == 1:
                return self._on(then, colls[0], items, who, last)
            if not colls:
                if then == "add":
                    return self._ask("Как назвать новый список?", op="new_name", kind=kind, items=items)
                return Reply("Списков пока нет." if kind == "list" else "Журналов пока нет.")
            verb = {"add": "В какой список добавить", "read": "Какой список прочитать"}.get(then, "Какой список")
            return self._ask(f"{verb}: {self._names(colls)}?", op="choose", kind=kind, then=then, items=items, last=last,
                             ids=[c["id"] for c in colls])
        found = self.find(query, kind)
        if len(found) == 1:
            return self._on(then, found[0], items, who, last)
        name = _join(query)
        if found:
            return self._ask(f"Какой: {self._names(found)}?", op="choose", kind=kind, then=then, items=items, last=last,
                             ids=[c["id"] for c in found])
        close = self.similar(query, kind)
        if then == "add":
            if len(close) == 1:
                return self._ask(
                    f"Такого нет, есть {self._to_title(close[0])}. Добавить туда?",
                    op="use_or_new", id=close[0]["id"], kind=kind, name=name, items=items,
                )
            return self._ask(f"{word.capitalize()} {name} нет. Создать?", op="new", kind=kind, name=name, items=items)
        others = f" Есть: {', '.join(c['name'] for c in colls if c['name'])}." if any(c["name"] for c in colls) else ""
        return Reply(f"{word.capitalize()} {name} нет.{others}")

    # -------------------------------------------------------- reading phrases
    @staticmethod
    def _marker(low: list[str]) -> tuple[int, str, int] | None:
        """(index of the word, kind, where the name starts)."""
        for i, w in enumerate(low):
            if w in _LIST_WORDS:
                return i, "list", i + 1
            if w in _BUY_WORDS:
                return i, "list", i
            if w.startswith(("заметк", "заметок")):
                return i, "notes", i + 1
            if w.startswith("журнал"):
                return i, "journal", i + 1
        return None

    def handle(self, text: str, who: str | None = None) -> Reply | None:
        """None: this phrase is not understood here (often it is not about lists at all)."""
        toks = _tokens(text)
        if len(toks) > MAX_WORDS:
            return None
        while toks and _low(toks[0]) in _FILLERS:
            toks = toks[1:]
        if not toks:
            return None
        low = [_low(t) for t in toks]
        verb = low[0]
        mark = self._marker(low)
        if mark is None:
            return self._unmarked(toks, low, who)
        idx, kind, start = mark
        head = toks[1:idx]
        while head and _low(head[-1]) in _PREPS:
            head = head[:-1]
        before = low[idx - 1] if idx else ""
        tail = toks[start:]
        is_add = verb.startswith(_ADD)
        is_read = verb in _ASK or verb.startswith(_READ) or idx == 0
        is_remove = verb.startswith(_REMOVE)
        is_clear = verb.startswith(_CLEAR)
        is_create = verb.startswith(_CREATE)
        last = any(w.startswith(_LAST) for w in low)
        if kind == "notes":
            return self._notes_phrase(toks, low, tail, idx, is_add, is_read, is_remove, is_clear, is_create, last, who)
        if kind == "journal":
            return self._journal_phrase(low, tail, idx, is_add, is_read, is_remove, is_clear, is_create, last, who)
        plural = low[idx] in _LIST_PLURAL and not _content(tail)
        if plural and (is_read or verb in _ASK):
            return self._overview("list")
        if is_create:
            return self._create(tail, who)
        if is_add:
            name, rest = self._resolve(tail)
            items = split_items(_join(head)) + split_items(_join(rest))
            return self._act("add", "list", name, items, who)
        if is_clear:
            return self._act("clear", "list", tail, [], who)
        if is_remove:
            if last or head:
                return self._act("remove", "list", tail, split_items(_join(head)), who, last)
            if before in ("из", "со", "с"):
                name, rest = self._resolve(tail)
                return self._act("remove", "list", name, split_items(_join(rest)), who)
            return self._act("delete", "list", tail, [], who)
        if is_read:
            return self._act("read", "list", tail, [], who)
        return None

    def _create(self, tail: list[str], who: str | None) -> Reply:
        if not _content(tail):
            return self._ask("Как назвать новый список?", op="new_name", kind="list", items=[])
        name, rest = (tail, []) if not self.find(tail) else self._resolve(tail)
        found = self.find(name)
        if found:
            return self._on("add", found[0], split_items(_join(rest)), who) if rest else self._ask(
                f"Такой список уже есть. Что добавить в {self._to_title(found[0])}?", op="items", id=found[0]["id"]
            )
        coll = self.create(_join(name), "list")
        return self._ask(f"Создан {self._to_title(coll)}. Что в него добавить?", op="items", id=coll["id"])

    def _notes_phrase(self, toks, low, tail, idx, is_add, is_read, is_remove, is_clear, is_create, last, who) -> Reply | None:
        plural = low[idx] != "заметка" and low[idx] != "заметку"
        text = _join(tail)
        if is_remove or is_clear:
            coll = self._single("notes")
            if coll is None:
                return Reply("Заметок пока нет.")
            if is_clear or (plural and not _content(tail) and not last):
                return self._on("clear", coll, [], who)
            if last or not _content(tail):
                return self._do_remove(coll, [], True)
            return self._do_remove(coll, [text], False)
        if is_add or is_create or (idx == 0 and not plural):
            if not _content(tail):
                return self._ask("Что записать?", op="text", kind="notes")
            return self._do_add(self._default("notes"), [text], who)
        if is_read:
            coll = self._single("notes")
            return self._do_read(coll) if coll else Reply("Заметок пока нет.")
        return None

    def _journal_phrase(self, low, tail, idx, is_add, is_read, is_remove, is_clear, is_create, last, who) -> Reply | None:
        named: list[dict[str, Any]] = []
        rest = tail
        # a journal is only named when it already exists: the rest of the phrase is the entry
        for k in range(len(tail), 0, -1):
            found = self.find(tail[:k], "journal")
            if _content(tail[:k]) and found:
                named, rest = found, tail[k:]
                break
        if low[idx] in ("журналы", "журналов") and not _content(tail) and (is_read or low[0] in _ASK):
            return self._overview("journal")
        if is_create:
            if not _content(tail):
                coll = self._default("journal")
            else:
                coll = named[0] if named else self.create(_join(tail), "journal")
            return self._ask(f"Журнал готов. Что записать в {self._to_title(coll)}?", op="items", id=coll["id"])
        coll = named[0] if named else self._single("journal")
        if is_add:
            target = coll if coll is not None else self._default("journal")
            if not _content(rest):
                return self._ask("Что записать?", op="items", id=target["id"])
            return self._do_add(target, [_join(rest)], who)
        if is_clear or is_remove:
            if coll is None:
                return Reply("Журналов пока нет.")
            if last:
                return self._do_remove(coll, [], True)
            return self._on("clear" if is_clear else "delete", coll, [], who)
        if is_read:
            if coll is None and self.of_kind("journal"):
                coll = self.of_kind("journal")[0] if len(self.of_kind("journal")) == 1 else None
            return self._do_read(coll) if coll else Reply("Журнал пока пуст.")
        return None

    def _unmarked(self, toks: list[str], low: list[str], who: str | None) -> Reply | None:
        verb = low[0]
        if verb.startswith(_NOTE_VERBS):
            rest = toks[1:]
            if rest and _low(rest[0]) == "что":
                rest = rest[1:]
            if not _content(rest):
                return self._ask("Что записать?", op="text", kind="notes")
            return self._do_add(self._default("notes"), [_join(rest)], who)
        # «что купить на дачу», «что надо купить»
        if verb == "что" and "купить" in low:
            tail = toks[low.index("купить") + 1:]
            if _content(tail):
                return self._act("read", "list", tail, [], who)
            return self._act("read", "list", ["покупок"], [], who)
        # «добавь лопату на дачу» counts only when such a list exists: «добавь яркость на кухне» is not about lists
        if verb.startswith(_ADD):
            for i in range(1, len(toks) - 1):
                if low[i] in _PREPS and i > 1 and self.find(toks[i:]):
                    return self._act("add", "list", toks[i:], split_items(_join(toks[1:i])), who)
        return None

    # ------------------------------------------------------ the answer to a question
    def answer(self, text: str, pending: dict[str, Any], who: str | None = None) -> Reply | None:
        """The phrase after a question. None: it is another command, the question is dropped."""
        toks = _tokens(text)
        low = [_low(t) for t in toks]
        if not low:
            return None
        if low[0].startswith(_DEVICE_VERBS) or asked_measure(text):
            return None
        op = pending.get("op")
        first = low[0]
        says_no = first in _NO
        says_yes = first in _YES
        coll = self.get(pending["id"]) if pending.get("id") else None
        if pending.get("id") and coll is None:
            return Reply("Этого уже нет.")
        if op in ("items", "text", "remove_what") and self._marker(low) is not None and len(low) > 1 and not says_no:
            return None     # a new command about lists, not the answer
        if op == "items":
            if says_no:
                return Reply("Хорошо, ничего не добавляю.")
            if first.startswith(_ADD):
                text = _join(toks[1:])
            if coll["kind"] == "list":
                return self._do_add(coll, split_items(text), who)
            return self._do_add(coll, [text.strip(_PUNCT + " ")], who)
        if op == "text":
            if says_no:
                return Reply("Хорошо, не записываю.")
            return self._do_add(self._default(pending.get("kind") or "notes"), [text.strip(_PUNCT + " ")], who)
        if op == "remove_what":
            if says_no:
                return Reply("Хорошо, ничего не убираю.")
            return self._do_remove(coll, split_items(text))
        if op == "choose":
            if says_no:
                return Reply("Хорошо.")
            ids = pending.get("ids") or []
            query = _content(toks)
            hits = [c for c in (self.get(i) for i in ids) if c and query and any(_word_in(q, _content(_tokens(c["name"]))) for q in query)]
            if len(hits) != 1:
                return Reply("Не поняла, какой. Повторите команду целиком.")
            return self._on(pending.get("then") or "read", hits[0], pending.get("items") or [], who, bool(pending.get("last")))
        if op == "new_name":
            if says_no:
                return Reply("Хорошо, не создаю.")
            return self._made(_join(toks), pending, who)
        if op == "new":
            if says_no:
                return Reply("Хорошо, не создаю.")
            if says_yes:
                return self._made(pending.get("name") or "", pending, who)
            # another name instead of «да»
            return self._act("add", pending.get("kind") or "list", toks, pending.get("items") or [], who)
        if op == "use_or_new":
            if says_yes:
                return self._on("add", coll, pending.get("items") or [], who)
            if says_no:
                return self._ask(
                    f"Создать новый список {pending.get('name')}?", op="new", kind=pending.get("kind") or "list",
                    name=pending.get("name") or "", items=pending.get("items") or [],
                )
            return self._act("add", pending.get("kind") or "list", toks, pending.get("items") or [], who)
        if op == "confirm":
            if not says_yes:
                return Reply("Хорошо, оставляю.")
            title = self._to_title(coll).capitalize()
            if pending.get("then") == "delete":
                self.delete(coll["id"])
                return Reply(f"{title} удалён.", actions=[{"service": "удалено", "target": self.title(coll)}])
            self._clear(coll)
            return Reply(f"{title} очищен.", actions=[{"service": "очищено", "target": self.title(coll)}])
        return None

    def _made(self, name: str, pending: dict[str, Any], who: str | None) -> Reply:
        coll = self.create(name, pending.get("kind") or "list")
        return self._on("add", coll, pending.get("items") or [], who)

    # --------------------------------------------------------- what the model asks for
    def names_line(self) -> str:
        lists = [c["name"] for c in self.of_kind("list")]
        return f"Есть: {', '.join(lists)}" if lists else "Есть: пока ни одного списка"

    def run_tool(self, name: str, args: dict[str, Any], who: str | None = None) -> Reply:
        query = _tokens(str(args.get("list") or ""))
        items = args.get("items")
        if isinstance(items, str):
            items = split_items(items)
        items = [str(i) for i in (items or []) if str(i).strip()]
        text = str(args.get("text") or "").strip()
        if name == "list_add":
            return self._act("add", "list", query, items, who)
        if name == "list_read":
            return self._act("read", "list", query, [], who)
        if name == "list_remove":
            return self._act("remove", "list", query, items, who)
        if name == "lists_overview":
            return self._overview("list")
        if name in ("note_add", "journal_add"):
            kind = "notes" if name == "note_add" else "journal"
            if not text:
                return self._ask("Что записать?", op="text", kind=kind)
            return self._do_add(self._default(kind), [text], who)
        if name in ("notes_read", "journal_read"):
            coll = self._single("notes" if name == "notes_read" else "journal")
            return self._do_read(coll) if coll else Reply("Пока ничего не записано.")
        if name == "ask_user":
            return self._ask(str(args.get("question") or "Уточните, пожалуйста."), op="again")
        return Reply("Не поняла, что сделать.")

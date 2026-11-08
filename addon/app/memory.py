"""What was said lately, for «сколько ты говорила варить гречку?».

Every answered phrase is kept: the question, the answer, who and when. The model
gets them with the next question in a block «Память»: the last minutes almost
word for word, older ones by a one-line summary, and after a few hours nothing.
The summaries are written by the model between requests, while the house is
quiet; a new command stops that at once (see Assistant.schedule_memory).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

_LOGGER = logging.getLogger("ha-voice.memory")

# the last minutes: the answer as it was said
FRESH_S = 600
# older ones: their summary; after this they are forgotten
KEEP_S = 6 * 3600
MAX_ITEMS = 40
# one answer word for word, at most
FULL_CHARS = 700
# an answer this short is its own summary
SHORT_CHARS = 160
# the whole block: a CPU reads every character of it before answering
BLOCK_CHARS = 2200


@dataclass
class Exchange:
    at: float
    question: str
    answer: str
    who: str = ""
    source: str = ""
    summary: str = ""

    @property
    def needs_summary(self) -> bool:
        return not self.summary and len(self.answer) > SHORT_CHARS

    def short(self) -> str:
        if self.summary:
            return self.summary
        return _cut(self.answer, SHORT_CHARS)


def _cut(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > limit // 2 else cut.rstrip() + "…").strip()


def _same(a: str, b: str) -> bool:
    norm = lambda x: re.sub(r"[^\wа-яё]+", " ", (x or "").lower()).strip()  # noqa: E731
    return norm(a) == norm(b)


class Memory:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.items: list[Exchange] = []
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [Exchange(**x) for x in data.get("items", []) if isinstance(x, dict)]
            self._prune()
        except (OSError, ValueError, TypeError) as err:
            _LOGGER.warning("Memory not read: %s", err)

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"items": [asdict(x) for x in self.items]}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as err:
            _LOGGER.warning("Memory not saved: %s", err)

    def _prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self.items = [x for x in self.items if now - x.at < KEEP_S][-MAX_ITEMS:]

    def add(self, question: str, answer: str, who: str = "", source: str = "") -> None:
        question, answer = (question or "").strip(), (answer or "").strip()
        if not question or not answer:
            return
        self.items.append(Exchange(time.time(), question, _cut(answer, FULL_CHARS * 2), who or "", source or ""))
        self._prune()
        self.save()

    def clear(self) -> None:
        self.items = []
        self.save()

    def pending(self) -> list[Exchange]:
        """Long answers without a summary yet, oldest first."""
        return [x for x in self.items if x.needs_summary]

    def block(self, skip: list[str] | None = None, now: float | None = None) -> str:
        """The «Память» text for the model; skip: questions it already has in the talk itself."""
        now = time.time() if now is None else now
        self._prune(now)
        skip = skip or []
        lines: list[str] = []
        used = 0
        for x in reversed(self.items):
            if any(_same(x.question, s) for s in skip):
                continue
            fresh = now - x.at < FRESH_S
            answer = _cut(x.answer, FULL_CHARS) if fresh else x.short()
            when = datetime.fromtimestamp(x.at).strftime("%H:%M")
            who = f", {x.who}" if x.who else ""
            line = f"- {when}{who}: «{_cut(x.question, 200)}» → {'' if fresh else 'кратко: '}«{answer}»"
            if used + len(line) > BLOCK_CHARS:
                if not fresh or not x.short():
                    break
                line = f"- {when}{who}: «{_cut(x.question, 200)}» → кратко: «{x.short()}»"
                if used + len(line) > BLOCK_CHARS:
                    break
            lines.append(line)
            used += len(line)
        if not lines:
            return ""
        return "Память — недавний разговор, сначала новое:\n" + "\n".join(lines) + "\nВопрос:\n"

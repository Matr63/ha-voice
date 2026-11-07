"""«Температура в зале»: which sensor is meant, found by rooms and hints, without the LLM.

A small local model invents entity ids and numbers. Reading a value needs no
intelligence: the measure comes from the words, the place from room names, room
hints, device names and entity hints. Anything else (commands, weather, questions
about the past) returns None and goes to the LLM.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# measure -> stems in the phrase, stems in an entity's name or id, units that prove it
MEASURES: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "температура": (("температур", "градус"), ("температур", "temperat"), ("°C", "°F")),
    "влажность": (("влажност",), ("влажност", "humid", "vlazhnost"), ()),
    "давление": (("давлени",), ("давлени", "pressure", "davlenie"), ("hPa", "гПа", "mmHg", "мм рт. ст.")),
    "заряд": (("батаре", "батарей", "заряд", "аккумулятор"), ("батаре", "batter", "batareia", "заряд"), ()),
    "освещённость": (("освещенност", "освещени"), ("освещен", "illuminance", "osveshch"), ("lx",)),
}
# not a question about the current value of a sensor
_ACTION_STEMS = (
    "установ", "постав", "сделай", "сделать", "включ", "выключ", "убав", "прибав", "подними", "подня", "опусти",
    "пониз", "повыс", "измени", "настрой", "увелич", "уменьш",
)
_OTHER_STEMS = (
    "почему", "зачем", "когда", "вчера", "завтра", "будет", "была", "был", "прогноз", "погод", "улиц", "снаружи",
    "средн", "минимал", "максимал", "выше", "ниже", "больше", "меньше", "разниц", "сравн",
)
_PREPS = {"в", "во", "внутри", "на", "у", "около", "возле", "рядом", "под", "над", "за", "при", "с", "со", "из"}
_INSIDE = {"в", "во", "внутри"}
_STOP = {
    "какая", "какой", "какое", "какие", "какую", "сколько", "сейчас", "скажи", "скажите", "покажи", "подскажи", "узнай",
    "проверь", "там", "тут", "здесь", "а", "и", "ну", "мне", "пожалуйста", "дома", "доме", "дом", "это", "что", "как",
    "есть", "сегодня", "текущая", "текущую", "значение", "показания", "датчик", "датчика", "датчике", "датчиков",
    "комната", "комнате", "комнаты", "этой", "этом", "нас", "тебя", "меня", "же", "ли", "по", "от", "о",
}
MAX_WORDS = 10
MAX_ANSWERS = 2


@dataclass
class Lookup:
    measure: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    missing: str = ""                # the place was named, but there is no such sensor
    scores: dict[str, int] = field(default_factory=dict)


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def _words(text: str) -> list[str]:
    # T1 and Т1 sound the same: the latin letter of a sensor's short name is not told apart
    return [w.replace("t", "т") if any(c.isdigit() for c in w) else w for w in re.findall(r"[а-яa-z0-9]+", _norm(text))]


def _first_sentence(text: str) -> str:
    parts = [p.strip() for p in re.split(r"[.!?…\n]+", text or "") if p.strip()]
    return parts[0] if parts else ""


def _has_stem(words: list[str], stems: tuple[str, ...]) -> bool:
    return any(w.startswith(stems) for w in words)


def _same_word(a: str, b: str) -> bool:
    """Same word in another case: кухне/кухня, зал/зале, гостиной/гостинная."""
    if a == b:
        return True
    if min(len(a), len(b)) < 3:
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(3, max(len(a), len(b)) - 3)


def asked_measure(text: str) -> str | None:
    """The measure the phrase asks about, unless it is a command."""
    words = _words(_first_sentence(text))
    if _has_stem(words, _ACTION_STEMS):
        return None
    return next((m for m, (stems, _, _) in MEASURES.items() if _has_stem(words, stems)), None)


def _place_tokens(words: list[str], measure: str) -> list[tuple[str, str]]:
    """(word, its preposition) for the words that can name a place."""
    stems = MEASURES[measure][0]
    tokens: list[tuple[str, str]] = []
    prep = ""
    for w in words:
        if w in _PREPS:
            prep = w
        elif w in _STOP or w.startswith(stems):
            prep = ""
        else:
            tokens.append((w, prep))
            prep = ""
    return tokens


def _is_measure(row: dict[str, Any], measure: str) -> bool:
    _, names, units = MEASURES[measure]
    if row.get("unit") and row["unit"] in units:
        return True
    return any(n in _norm(f"{row.get('name', '')} {row.get('entity_id', '')}") for n in names)


def find_sensors(text: str, rows: list[dict[str, Any]], hints: dict[str, str], room_hints: dict[str, str], room: str | None) -> Lookup | None:
    """rows: sensors the assistant may see. hints: entity_id -> its hints. room: where the board is."""
    sentence = _first_sentence(text)
    words = _words(sentence)
    if not words or len(words) > MAX_WORDS:
        return None
    if _has_stem(words, _ACTION_STEMS) or _has_stem(words, _OTHER_STEMS) or any(w.isdigit() for w in words):
        return None
    measure = asked_measure(sentence)
    if measure is None:
        return None
    sensors = [r for r in rows if r.get("domain") == "sensor" and _is_measure(r, measure)]
    if not sensors:
        return None

    def room_words(r: dict[str, Any]) -> list[str]:
        area = r.get("area") or ""
        return [w for w in _words(f"{area} {room_hints.get(area, '')}") if w not in _PREPS and w not in _STOP]

    def own_words(r: dict[str, Any]) -> list[str]:
        stems = MEASURES[measure][1] + MEASURES[measure][0]
        text_ = f"{hints.get(r['entity_id'], '')} {r.get('device') or ''} {r.get('name') or ''}"
        return [w for w in _words(text_) if w not in _PREPS and w not in _STOP and not w.startswith(stems)]

    tokens = _place_tokens(words, measure)
    named = bool(tokens)
    if not named:
        # «какая температура?»: the room the board stands in
        if not room:
            return None
        tokens = [(w, "") for w in _words(f"{room} {room_hints.get(room, '')}") if w not in _PREPS and w not in _STOP]
        sensors = [r for r in sensors if (r.get("area") or "") == room] or sensors

    scores: dict[str, int] = {}
    matched: set[str] = set()
    for r in sensors:
        total = 0
        own, place = own_words(r), room_words(r)
        for token, prep in tokens:
            in_room = any(_same_word(token, w) for w in place)
            # «в холодильнике» is inside it; a sensor «у холодильника» does not count.
            # A room is different: «в гостиной» and the hint «гостиная» are the same place.
            exact = prep in _INSIDE and not in_room
            if any(token == w if exact else _same_word(token, w) for w in own):
                total += 2
                matched.add(token)
            if in_room:
                total += 1
                matched.add(token)
        scores[r["entity_id"]] = total
    if not named and not any(scores.values()):
        scores = {r["entity_id"]: 1 for r in sensors}

    missing = next((f"{prep} {token}" for token, prep in tokens if prep in _INSIDE and token not in matched), "")
    if named and missing:
        return Lookup(measure, missing=missing, scores=scores)
    best = max(scores.values())
    if best == 0:
        where = next((f"{prep} {token}" for token, prep in tokens if prep), "")
        return Lookup(measure, missing=where, scores=scores) if where else None
    top = [r for r in sensors if scores[r["entity_id"]] == best]
    return Lookup(measure, rows=top[:MAX_ANSWERS], scores=scores)


# ------------------------------------------------------------- spoken values
_ONES = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_ONES_F = ["", "одна", "две", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
          "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
# unit -> forms for 1, 2-4, 5-0
_UNITS = {
    "°C": ("градус", "градуса", "градусов"),
    "°F": ("градус Фаренгейта", "градуса Фаренгейта", "градусов Фаренгейта"),
    "%": ("процент", "процента", "процентов"),
    "hPa": ("гектопаскаль", "гектопаскаля", "гектопаскалей"),
    "гПа": ("гектопаскаль", "гектопаскаля", "гектопаскалей"),
    "lx": ("люкс", "люкса", "люксов"),
}


def _below_thousand(n: int, ones: list[str]) -> list[str]:
    out = [_HUNDREDS[n // 100]]
    rest = n % 100
    if 10 <= rest < 20:
        out.append(_TEENS[rest - 10])
    else:
        out += [_TENS[rest // 10], ones[rest % 10]]
    return [w for w in out if w]


def number_words(n: int) -> str:
    if n == 0:
        return "ноль"
    sign = ["минус"] if n < 0 else []
    n = abs(n)
    out: list[str] = []
    thousands = n // 1000
    if thousands:
        last, teen = thousands % 10, 10 <= thousands % 100 < 20
        word = "тысяч" if teen or last == 0 or last > 4 else ("тысяча" if last == 1 else "тысячи")
        out += _below_thousand(thousands % 1000, _ONES_F) + [word]
    out += _below_thousand(n % 1000, _ONES)
    return " ".join(sign + out)


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n)
    if 10 <= n % 100 < 20:
        return forms[2]
    last = n % 10
    return forms[0] if last == 1 else forms[1] if 2 <= last <= 4 else forms[2]


def spoken_value(state: Any, unit: str = "", measure: str = "") -> str:
    """24.52, °C -> «двадцать четыре и пять десятых градуса». The speech engine gets words, not digits."""
    try:
        value = float(state)
    except (TypeError, ValueError):
        return f"{state} {unit}".strip()
    scaled = round(abs(value) * 10) if measure == "температура" else round(abs(value)) * 10
    whole, tenths = divmod(scaled, 10)
    number = number_words(-whole if value < 0 else whole)
    forms = _UNITS.get(unit)
    if not tenths:
        return f"{number} {_plural(whole, forms) if forms else unit}".strip()
    if tenths == 1:
        fraction = "одна десятая"
    elif tenths == 2:
        fraction = "две десятых"
    else:
        fraction = f"{_ONES_F[tenths]} десятых"
    return f"{number} и {fraction} {forms[1] if forms else unit}".strip()

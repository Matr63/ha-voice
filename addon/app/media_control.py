"""Media player commands the way a small model writes them, made into what HA accepts.

The model says «pause» and «volume 100»; HA knows media_pause and a level from 0 to 1.
One TV is often two entities too (Android TV Remote, Cast), and each can do what the
other cannot: the remote has no absolute volume, so the command goes to the twin that has.
"""
from __future__ import annotations

from typing import Any, Callable

SERVICE_ALIASES = {
    "play": "media_play",
    "resume": "media_play",
    "unpause": "media_play",
    "pause": "media_pause",
    "stop": "media_stop",
    "play_pause": "media_play_pause",
    "next": "media_next_track",
    "next_track": "media_next_track",
    "previous": "media_previous_track",
    "prev": "media_previous_track",
    "previous_track": "media_previous_track",
    "mute": "volume_mute",
    "set_volume": "volume_set",
    "volume": "volume_set",
}
# MediaPlayerEntityFeature bits: any of them makes the service work
_PAUSE, _VOLUME_SET, _MUTE, _PREVIOUS, _NEXT, _VOLUME_STEP, _STOP, _PLAY = 1, 4, 8, 16, 32, 1024, 4096, 16384
SERVICE_FEATURES = {
    "media_play": _PLAY,
    "media_pause": _PAUSE,
    "media_play_pause": _PLAY | _PAUSE,
    "media_stop": _STOP,
    "media_next_track": _NEXT,
    "media_previous_track": _PREVIOUS,
    "volume_set": _VOLUME_SET,
    "volume_up": _VOLUME_SET | _VOLUME_STEP,
    "volume_down": _VOLUME_SET | _VOLUME_STEP,
    "volume_mute": _MUTE,
}
_VOLUME_KEYS = ("volume_level", "volume", "level", "volume_pct", "percent", "value")


def service_name(service: str) -> str:
    return SERVICE_ALIASES.get(service, service)


def service_data(service: str, data: dict[str, Any]) -> dict[str, Any]:
    """The service's data as HA wants it. ValueError: what to tell the model."""
    if service == "volume_mute":
        return {"is_volume_muted": True, **data}
    if service != "volume_set":
        return data
    raw = next((data[k] for k in _VOLUME_KEYS if data.get(k) is not None), None)
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("Для volume_set нужен data.volume_level: проценты от 0 до 100") from None
    if not 0 <= value <= 100:
        raise ValueError(f"Громкость бывает от 0 до 100, а не {raw}. Скажи об этом пользователю")
    # percent, and 1 is one percent: a misread number must not blast the room
    return {"volume_level": round(value if value < 1 else value / 100, 3)}


def supports(service: str, state: dict[str, Any]) -> bool:
    """Unknown service or no feature list (say, the player is unavailable): HA answers for itself."""
    needed = SERVICE_FEATURES.get(service)
    features = (state.get("attributes") or {}).get("supported_features")
    return needed is None or not isinstance(features, int) or bool(features & needed)


def _same_device_name(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return bool(a.get("device")) and a.get("device") == b.get("device") and a.get("area") == b.get("area")


def twins(entity_id: str, rows: list[dict[str, Any]]) -> list[str]:
    """Other players of the same name in the same room: the one TV through another integration."""
    own = next((r for r in rows if r["entity_id"] == entity_id), None)
    if own is None:
        return []
    return [
        r["entity_id"]
        for r in rows
        if r.get("domain") == "media_player" and r["entity_id"] != entity_id and _same_device_name(r, own)
    ]


def without_unnamed_twins(rows: list[dict[str, Any]], hints: Callable[[str], str]) -> list[dict[str, Any]]:
    """Out of the prompt: a player with no hints whose twin has them. Two identical «Smart TV»
    make the model answer «which one?»; a command the named one cannot do still reaches the twin."""
    named = [r for r in rows if r.get("domain") == "media_player" and hints(r["entity_id"])]

    def hidden(r: dict[str, Any]) -> bool:
        if r.get("domain") != "media_player" or hints(r["entity_id"]):
            return False
        return any(_same_device_name(r, n) for n in named)

    return [r for r in rows if not hidden(r)]

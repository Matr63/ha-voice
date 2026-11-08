"""Entity lists the assistant got from Home Assistant, kept as numbered snapshots.

The command log links to a snapshot instead of carrying the whole list. A new
snapshot is made only when the list itself changed: an entity added or removed,
renamed, moved to another room, or taken in or out of the model's catalog. A
changed sensor value alone does not make one (it changes every few seconds);
the values a snapshot shows are those of the moment it was made.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("ha-voice.snapshots")

KEEP_SNAPSHOTS = 30
# what makes the list "the same": everything but the value
_KEY_FIELDS = ("entity_id", "name", "area", "device", "unit", "exposed")
# actions: what a player takes, by its supported_features (a Cast twin changes them
# with the running app, so they are a value, not part of the list's identity)
_ROW_FIELDS = ("entity_id", "name", "state", "unit", "area", "device", "exposed", "actions")


# bumped when a snapshot starts carrying more: the first list after an update gets a new snapshot
_FORMAT = 2


def _digest(rows: list[dict[str, Any]]) -> str:
    key: list[Any] = [_FORMAT]
    key += sorted(tuple(str(r.get(f, "")) for f in _KEY_FIELDS) for r in rows)
    return hashlib.sha1(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()


class EntitySnapshots:
    def __init__(self, path: Path | None, keep: int = KEEP_SNAPSHOTS) -> None:
        self.path = path
        self.keep = keep
        self.items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [x for x in data.get("snapshots", []) if isinstance(x, dict) and "id" in x]
        except (OSError, ValueError) as err:
            _LOGGER.warning("Entity snapshots not read: %s", err)

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"snapshots": self.items}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as err:
            _LOGGER.warning("Entity snapshots not saved: %s", err)

    def add(self, rows: list[dict[str, Any]]) -> tuple[int, bool]:
        """(snapshot id, made just now). The same list as the last snapshot reuses its id."""
        digest = _digest(rows)
        if self.items and self.items[-1].get("digest") == digest:
            return int(self.items[-1]["id"]), False
        sid = int(self.items[-1]["id"]) + 1 if self.items else 1
        self.items.append({
            "id": sid,
            "at": time.time(),
            "digest": digest,
            "rows": [{f: r[f] for f in _ROW_FIELDS if f in r} for r in rows],
        })
        del self.items[: -self.keep]
        self._save()
        return sid, True

    @staticmethod
    def _head(item: dict[str, Any]) -> dict[str, Any]:
        rows = item.get("rows") or []
        return {
            "id": item["id"],
            "at": item.get("at"),
            "count": len(rows),
            "exposed": sum(1 for r in rows if r.get("exposed")),
        }

    def list(self) -> list[dict[str, Any]]:
        return [self._head(x) for x in reversed(self.items)]

    def get(self, sid: int) -> dict[str, Any] | None:
        """The snapshot with what changed since the one before it."""
        for i, item in enumerate(self.items):
            if int(item["id"]) != sid:
                continue
            out = {**self._head(item), "rows": item.get("rows") or []}
            if i > 0:
                prev = {r["entity_id"]: r for r in self.items[i - 1].get("rows") or []}
                cur = {r["entity_id"]: r for r in out["rows"]}
                key = [f for f in _KEY_FIELDS if f != "entity_id"]
                out["previous"] = self.items[i - 1]["id"]
                out["added"] = sorted(set(cur) - set(prev))
                out["removed"] = sorted(set(prev) - set(cur))
                out["changed"] = sorted(
                    e for e in set(cur) & set(prev) if any(cur[e].get(f) != prev[e].get(f) for f in key)
                )
            if i + 1 < len(self.items):
                out["next"] = self.items[i + 1]["id"]
            return out
        return None

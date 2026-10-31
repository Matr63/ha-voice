"""Sleep mode: the board keeps listening (diary), but the wake word is ignored.

Sources, strongest first:
  - a voice / panel / HA override ("не спи", "спи", the «Сон» switch) that
    lasts until the schedule would change on its own
  - the schedule (sleep_start .. sleep_end, can cross midnight)
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from settings import SettingsStore


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


class Modes:
    def __init__(self, settings: SettingsStore) -> None:
        self.settings = settings
        self._override: tuple[bool, float, str] | None = None  # (sleep, until ts, source)

    def _schedule(self, now: datetime) -> tuple[bool, datetime | None]:
        """(asleep by schedule, next time the schedule flips)."""
        s = self.settings.get()
        if not s.sleep_schedule:
            return False, None
        (sh, sm), (eh, em) = _hm(s.sleep_start), _hm(s.sleep_end)
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        if start == end:
            return False, None
        if start < end:
            asleep = start <= now < end
        else:  # crosses midnight
            asleep = now >= start or now < end
        boundaries = [t + timedelta(days=d) for t in (start, end) for d in (0, 1)]
        nxt = min(t for t in boundaries if t > now)
        return asleep, nxt

    def sleeping(self) -> bool:
        return self.status()["sleeping"]

    def status(self) -> dict[str, Any]:
        now = datetime.now()
        asleep, nxt = self._schedule(now)
        if self._override is not None:
            on, until, source = self._override
            if time.time() < until:
                return {
                    "sleeping": on,
                    "source": source,
                    "until": datetime.fromtimestamp(until).isoformat(timespec="minutes") if until != float("inf") else None,
                }
            self._override = None
        return {
            "sleeping": asleep,
            "source": "schedule" if self.settings.get().sleep_schedule else "default",
            "until": nxt.isoformat(timespec="minutes") if nxt else None,
        }

    def set_sleep(self, on: bool, source: str) -> dict[str, Any]:
        """Override until the schedule changes by itself (or for good without a schedule)."""
        now = datetime.now()
        asleep, nxt = self._schedule(now)
        if self.settings.get().sleep_schedule and asleep == on:
            self._override = None
        else:
            self._override = (on, nxt.timestamp() if nxt else float("inf"), source)
        return self.status()

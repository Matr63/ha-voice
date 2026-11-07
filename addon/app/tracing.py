"""The trace of one command: everything sent to the model or HA and what came back.

Kept in a context variable, so HA calls made deep inside the client land in the
trace of the command that caused them.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from typing import Any

_LOGGER = logging.getLogger("ha-voice.assistant")

_trace: ContextVar[list[dict[str, Any]] | None] = ContextVar("assistant_trace", default=None)
LOGGED_KINDS = ("llm_reply", "tool", "direct", "ha_reply", "ha_call")


def start() -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []
    _trace.set(trace)
    return trace


def active() -> bool:
    return _trace.get() is not None


def note(kind: str, **data: Any) -> None:
    trace = _trace.get()
    if trace is not None:
        trace.append({"kind": kind, **data})
    if kind in LOGGED_KINDS:
        _LOGGER.info("%s: %s", kind, json.dumps(data, ensure_ascii=False, default=str)[:800])

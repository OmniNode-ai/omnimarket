# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reduce a Claude stream-json log to the first-turn context by source (OMN-21889)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from ..models.model_remote_lane_first_turn_context import (
    ModelRemoteLaneFirstTurnContext,
)
from ..models.model_remote_lane_receipt_usage import UsageCount

COLUMNS = tuple(ModelRemoteLaneFirstTurnContext.model_fields)


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: object) -> UsageCount:
    return value if type(value) is int and value >= 0 else "UNKNOWN"


def _usage_context(usage: dict[str, Any]) -> UsageCount:
    parts = [
        _count(usage.get(key))
        for key in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    ]
    if any(part == "UNKNOWN" for part in parts):
        return "UNKNOWN"
    return sum(part for part in parts if isinstance(part, int))


def parse_engine_log(text: str) -> tuple[dict[str, Any], ...]:
    """The JSON object lines of a lane log; wrapper lines and prose are skipped."""
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return tuple(events)


def first_turn_context(
    events: Iterable[dict[str, Any]] | None, brief: str | None
) -> ModelRemoteLaneFirstTurnContext:
    """Measure each field from the events; what the log does not hold stays UNKNOWN."""
    values: dict[str, Any] = {}
    if brief is not None:
        values["brief_bytes"] = len(brief.encode())
    turns: dict[str, UsageCount] = {}
    first_usage: dict[str, Any] | None = None
    for index, event in enumerate(events or ()):
        if event.get("type") == "system" and event.get("subtype") == "init":
            for source, target in (
                ("tools", "tools_count"),
                ("plugins", "plugins_count"),
            ):
                if isinstance(event.get(source), list):
                    values[target] = len(event[source])
            skills = event.get("skills")
            if isinstance(skills, list):
                values["skill_listing_count"] = len(skills)
                values["skill_listing_bytes"] = len(
                    "\n".join(str(skill) for skill in skills).encode()
                )
        elif event.get("type") == "assistant":
            message = _object(event.get("message"))
            key = str(message.get("id") or f"assistant-{index}")
            usage = _object(message.get("usage"))
            if usage and first_usage is None:
                first_usage = usage
            if usage:
                turns[key] = _usage_context(usage)
            else:
                turns.setdefault(key, "UNKNOWN")
    if turns:
        values["total_turns"] = len(turns)
        known = [value for value in turns.values() if isinstance(value, int)]
        if known:
            values["peak_context_tokens"] = max(known)
    if first_usage is not None:
        values["first_turn_input_tokens"] = _count(first_usage.get("input_tokens"))
        values["first_turn_cache_read"] = _count(
            first_usage.get("cache_read_input_tokens")
        )
        values["first_turn_cache_write"] = _count(
            first_usage.get("cache_creation_input_tokens")
        )
        values["first_turn_context_tokens"] = _usage_context(first_usage)
    return ModelRemoteLaneFirstTurnContext.model_validate(values)


def first_turn_context_table(
    lanes: Iterable[tuple[str, str, str | None]],
) -> str:
    """One markdown row per (lane, log text, brief) with the receipt's columns."""
    lines = [
        "| lane | " + " | ".join(COLUMNS) + " |",
        "|" + "---|" * (len(COLUMNS) + 1),
    ]
    for lane, log_text, brief in lanes:
        context = first_turn_context(parse_engine_log(log_text), brief).model_dump()
        lines.append(
            f"| {lane} | " + " | ".join(str(context[c]) for c in COLUMNS) + " |"
        )
    return "\n".join(lines) + "\n"

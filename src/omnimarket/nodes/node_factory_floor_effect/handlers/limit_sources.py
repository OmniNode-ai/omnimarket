# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the sources of the floor's Limits panel (OMN-22010): the token pool, the API credit meter, the engine
readings and the previous record's idle slots.

A source that is absent, unreadable or malformed comes back as ``None`` with the cause in words, never as an empty
reading: the panel then draws that source UNKNOWN, not zero.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnimarket.models.model_floor_limits_input import (
    ModelFloorApiKeySpend,
    ModelFloorApiMeter,
    ModelFloorEngineReadings,
    ModelFloorHostTokenPool,
    ModelFloorIdleCarry,
    ModelFloorIdleHostReading,
    ModelFloorIdleReading,
    ModelFloorLabModelRung,
    ModelFloorPoolToken,
    ModelFloorUsageWindow,
)
from omnimarket.models.model_merged_running_count import (
    ModelMergedRunningCountResult,
)


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"{path.name} does not exist"
    except (OSError, ValueError) as exc:
        return None, f"{path.name} is unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name} does not hold a JSON object"
    return data, ""


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _number(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def load_token_pool(
    path: Path,
) -> tuple[tuple[ModelFloorHostTokenPool, ...] | None, str]:
    """``{"hosts": {<host>: {"read_at", "tokens": [{"account", "state", "limited_until"}]}}}``."""
    data, cause = _read_json(path)
    if data is None:
        return None, cause
    try:
        return (
            tuple(
                ModelFloorHostTokenPool(
                    host=host,
                    read_at=_text(_mapping(entry).get("read_at")),
                    tokens=tuple(
                        ModelFloorPoolToken(
                            account=_text(t.get("account")),
                            state=t.get("state"),
                            limited_until=_text(t.get("limited_until")),
                        )
                        for t in _mapping(entry).get("tokens") or ()
                    ),
                )
                for host, entry in sorted(_mapping(data.get("hosts")).items())
            ),
            "",
        )
    except (ValidationError, AttributeError, TypeError) as exc:
        return None, f"{path.name} is malformed: {exc}"


def load_api_meter(path: Path) -> tuple[ModelFloorApiMeter | None, str]:
    """``{"read_at", "keys": {<name>: {"month": "YYYY-MM", "month_spend_usd": <number>}}}``."""
    data, cause = _read_json(path)
    if data is None:
        return None, cause
    try:
        return (
            ModelFloorApiMeter(
                read_at=_text(data.get("read_at")),
                keys=tuple(
                    ModelFloorApiKeySpend(
                        name=name,
                        month=_text(_mapping(entry).get("month")),
                        month_spend_usd=_number(_mapping(entry).get("month_spend_usd")),
                    )
                    for name, entry in sorted(_mapping(data.get("keys")).items())
                ),
            ),
            "",
        )
    except ValidationError as exc:
        return None, f"{path.name} is malformed: {exc}"


def _window(value: Any) -> ModelFloorUsageWindow | None:
    entry = _mapping(value)
    if not entry:
        return None
    return ModelFloorUsageWindow(
        read_at=_text(entry.get("read_at")),
        window=_text(entry.get("window")),
        used_pct=_number(entry.get("used_pct")),
        resets_at=_text(entry.get("resets_at")),
    )


def load_engines(path: Path) -> tuple[ModelFloorEngineReadings | None, str]:
    """``{"codex": window, "glm": window, "lab_models": {<rung>: {"read_at", "available", "last_smoke_at"}}}`` where a
    window is ``{"read_at", "window", "used_pct", "resets_at"}``."""
    data, cause = _read_json(path)
    if data is None:
        return None, cause
    try:
        return (
            ModelFloorEngineReadings(
                codex=_window(data.get("codex")),
                glm=_window(data.get("glm")),
                lab_models=tuple(
                    ModelFloorLabModelRung(
                        name=name,
                        read_at=_text(_mapping(entry).get("read_at")),
                        available=_mapping(entry).get("available")
                        if isinstance(_mapping(entry).get("available"), bool)
                        else None,
                        last_smoke_at=_text(_mapping(entry).get("last_smoke_at")),
                    )
                    for name, entry in sorted(_mapping(data.get("lab_models")).items())
                ),
            ),
            "",
        )
    except ValidationError as exc:
        return None, f"{path.name} is malformed: {exc}"


def load_idle_carry(path: Path) -> ModelFloorIdleCarry | None:
    """The previous record's idle slots; None when there is no previous record or it carries no Limits section."""
    data, _ = _read_json(path)
    if data is None:
        return None
    limits = _mapping(_mapping(data.get("floor")).get("limits"))
    idle = _mapping(limits.get("idle"))
    generated = limits.get("generated_at")
    if not idle or not isinstance(generated, str) or idle.get("state") != "read":
        return None
    try:
        total = idle.get("idle_total")
        return ModelFloorIdleCarry(
            generated_at=datetime.fromisoformat(generated),
            day=_text(idle.get("day")),
            idle_total=total if isinstance(total, int) else None,
            idle_slot_hours=_number(idle.get("idle_slot_hours")),
            unread_minutes=_number(idle.get("unread_minutes")) or 0.0,
        )
    except (ValidationError, ValueError):
        return None


def idle_reading(
    merged_running: ModelMergedRunningCountResult | None,
) -> ModelFloorIdleReading | None:
    """The idle slots of the merged-and-running count, on its Eastern day; None when that count was not read."""
    if merged_running is None or not merged_running.days:
        return None
    return ModelFloorIdleReading(
        day=merged_running.days[0].local_date,
        hosts=tuple(
            ModelFloorIdleHostReading(
                host=h.host, state=h.state, slots=h.slots, idle=h.idle
            )
            for h in merged_running.idle_hosts
        ),
        idle_total=merged_running.idle_total,
        idle_unread=merged_running.idle_unread,
    )

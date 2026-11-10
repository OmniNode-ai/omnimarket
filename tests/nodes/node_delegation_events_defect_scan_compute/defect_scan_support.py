# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the delegation_events defect scan tests: the overlay fixture, planted days and one request builder."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_delegation_events_defect_scan import (
    ModelDefectScanFact,
    ModelDefectScanOverlay,
    ModelDefectScanRequest,
)

NAME = "node_delegation_events_defect_scan_compute"
NOW = datetime(2026, 11, 9, 6, 15, tzinfo=UTC)
OVERLAY_FILE = (
    Path(__file__).resolve().parent / "fixtures" / "overlays" / NAME / "overlay.yaml"
)


def contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    return loaded


def resolve_handler() -> Any:
    """The handler class the packaged contract routes the operation to."""
    [entry] = contract()["handler_routing"]["handlers"]
    handler = entry["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])


def overlay_data() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(OVERLAY_FILE.read_text(encoding="utf-8"))
    return loaded


def overlay(**over: Any) -> ModelDefectScanOverlay:
    return ModelDefectScanOverlay.model_validate({**overlay_data(), **over})


def fact(
    minutes_ago: float = 120,
    *,
    ok: bool | None = True,
    cost: str | None = "0.0042",
    source_of_cost: str | None = "usage-read",
    tokens: int = 900,
    lane: str | None = "lane-a",
    source: str = "real",
    shadow: bool = False,
) -> ModelDefectScanFact:
    return ModelDefectScanFact(
        event_time=NOW - timedelta(minutes=minutes_ago),
        data_source=source,
        is_shadow=shadow,
        terminal_ok=ok,
        cost_usd=None if cost is None else Decimal(cost),
        cost_measurement_source=source_of_cost,
        tokens_total=tokens,
        caller_lane=lane,
    )


def clean_day(rows: int = 120) -> tuple[ModelDefectScanFact, ...]:
    """A day of rows with no defect: each ended, was priced from a read usage, and names its lane."""
    return tuple(fact(90 + i * 10) for i in range(rows))


def planted(defect: str, count: int = 1) -> tuple[ModelDefectScanFact, ...]:
    """A clean day with ``count`` rows of one defect planted into it."""
    bad: dict[str, ModelDefectScanFact] = {
        "missing_terminal": fact(300, ok=None),
        "cost_unread_as_zero": fact(300, cost="0", source_of_cost=None),
        "unattributed": fact(300, lane=None),
    }
    return (*clean_day(), *(bad[defect],) * count)


def request(**over: Any) -> ModelDefectScanRequest:
    fields: dict[str, Any] = {
        "now": NOW,
        "overlay": overlay(),
        "events": clean_day(),
        "events_unread_reason": None,
    }
    fields.update(over)
    return ModelDefectScanRequest.model_validate(fields)

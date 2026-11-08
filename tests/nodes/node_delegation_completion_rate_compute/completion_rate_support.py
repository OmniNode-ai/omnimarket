# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the delegation completion-rate tests: the overlay fixture, planted hours and one request builder."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_delegation_completion_rate_compute.models.model_delegation_completion_rate import (
    ModelDelegationCompletionRateRequest,
    ModelDelegationEventFact,
    ModelDelegationRateOverlay,
)

NAME = "node_delegation_completion_rate_compute"
NOW = datetime(2026, 11, 8, 7, 0, tzinfo=UTC)
OVERLAY_FILE = (
    Path(__file__).resolve().parent / "fixtures" / "overlays" / NAME / "overlay.yaml"
)
DEPLOYED = "local-heavy-reasoning"
HARNESS = "harness:claude-glm"


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


def overlay(**over: Any) -> ModelDelegationRateOverlay:
    return ModelDelegationRateOverlay.model_validate({**overlay_data(), **over})


def event(
    minutes_ago: float = 10,
    *,
    backend: str | None = DEPLOYED,
    ok: bool | None = True,
    gate: bool | None = True,
    source: str = "real",
    shadow: bool = False,
) -> ModelDelegationEventFact:
    return ModelDelegationEventFact(
        event_time=NOW - timedelta(minutes=minutes_ago),
        data_source=source,
        is_shadow=shadow,
        terminal_ok=ok,
        quality_gate_passed=gate,
        answering_backend=backend,
    )


def hour(
    deployed: int, harness: int, failed: int = 0
) -> tuple[ModelDelegationEventFact, ...]:
    """An hour of rows spread over the window: completions by the deployed lane and by the harness, then failures."""
    rows = [event(1 + i * 0.4, backend=DEPLOYED) for i in range(deployed)]
    rows += [event(1 + i * 0.4, backend=HARNESS) for i in range(harness)]
    rows += [
        event(2 + i * 0.4, ok=False, gate=None, backend=None) for i in range(failed)
    ]
    return tuple(rows)


def above_bound_hour() -> tuple[ModelDelegationEventFact, ...]:
    """136 completions (100 by the deployed lane, 36 by the harness) and no failure: the hour the deploy chain read."""
    return hour(100, 36)


def below_bound_hour() -> tuple[ModelDelegationEventFact, ...]:
    """14 completions, every one by the harness (a dead deployed lane), and 5 failures."""
    return hour(0, 14, failed=5)


def request(**over: Any) -> ModelDelegationCompletionRateRequest:
    fields: dict[str, Any] = {
        "now": NOW,
        "overlay": overlay(),
        "events": above_bound_hour(),
        "events_unread_reason": None,
    }
    fields.update(over)
    return ModelDelegationCompletionRateRequest.model_validate(fields)

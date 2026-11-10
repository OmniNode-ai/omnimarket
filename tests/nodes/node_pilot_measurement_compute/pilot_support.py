# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for piece 16 measurement tests: one good request and the field-level edits the tests make to it."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.models.model_pilot_measurement import (
    ModelPilotDeclaration,
    ModelPilotMeasurementRequest,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.handlers.handler_pilot_manifest_check import (
    compute_bar_hash,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.model_pilot_bar_criterion import (
    ModelPilotBarCriterion,
)

NAME = "node_pilot_measurement_compute"
NOW = "2026-11-10T18:00:00Z"
STARTED = "2026-11-10T16:00:00Z"
FROZEN = "2026-11-10T15:00:00Z"
FINISHED = "2026-11-10T17:30:00Z"
DEADLINE = 21600
BAR = [
    {"metric": "accuracy_gain", "comparator": "ge", "threshold": 0.10},
    {"metric": "planted_score", "comparator": "ge", "threshold": 5.0},
    {"metric": "ingest_hours", "comparator": "le", "threshold": 6.0},
]
GOOD_RESULTS = {"accuracy_gain": 0.2, "planted_score": 6.0, "ingest_hours": 1.0}


def contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    return loaded


def resolve_handler() -> Any:
    """The handler class the packaged contract routes the measurement operation to."""
    [entry] = contract()["handler_routing"]["handlers"]
    handler = entry["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])


def declaration(
    pilot_id: str = "memory",
    gating: tuple[str, ...] = ("planted_score",),
    deadline: int = DEADLINE,
) -> dict[str, Any]:
    return {
        "pilot_id": pilot_id,
        "bar": BAR,
        "gating_metrics": list(gating),
        "result_deadline_seconds": deadline,
    }


def declared_bar_hash(pilot_id: str = "memory") -> str:
    """The hash a pilot must report: the manifest check's own hash of the declared bar."""
    parsed = ModelPilotDeclaration.model_validate(declaration(pilot_id))
    return compute_bar_hash(parsed.bar)


def bar_of(*criteria: dict[str, Any]) -> tuple[ModelPilotBarCriterion, ...]:
    return tuple(ModelPilotBarCriterion.model_validate(c) for c in criteria)


def fact(
    pilot_id: str = "memory",
    *,
    bar_hash: str | None = "own",
    frozen_at: str | None = FROZEN,
    started_at: str = STARTED,
    results: dict[str, float] | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """A pilot's report. ``bar_hash="own"`` is the declared bar's hash; pass None for none or any other text."""
    return {
        "pilot_id": pilot_id,
        "bar_hash": declared_bar_hash(pilot_id) if bar_hash == "own" else bar_hash,
        "frozen_at": frozen_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "results": results,
    }


def finished(pilot_id: str = "memory", **results: float) -> dict[str, Any]:
    return fact(
        pilot_id,
        finished_at=FINISHED,
        results={**GOOD_RESULTS, **results},
    )


def good() -> dict[str, Any]:
    """One declared pilot that froze its declared bar before it started and met every bar."""
    return {
        "now": NOW,
        "declarations": [declaration()],
        "facts": [finished()],
        "facts_unread_reason": None,
    }


def request(**edits: Any) -> ModelPilotMeasurementRequest:
    return ModelPilotMeasurementRequest.model_validate({**good(), **edits})

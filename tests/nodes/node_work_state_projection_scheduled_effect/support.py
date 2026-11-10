# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the scheduled work-state projection's tests (OMN-22935)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.work_state_projection import (
    ModelWorkStateRules,
    ModelWorkStateScheduleOverlay,
)

T0 = datetime(2026, 11, 10, 3, 0, tzinfo=UTC)
FIXTURE = (
    Path(__file__).parent
    / "fixtures/overlays/node_work_state_projection_scheduled_effect/overlay.yaml"
)
RULES_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_work_state_projection_compute/overlay.example.yaml"
)
SRC = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="runtime-fixture",
        tick_interval_ms=1000,
    )


def schedule(**override: object) -> ModelWorkStateScheduleOverlay:
    base = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    return ModelWorkStateScheduleOverlay.model_validate(base | override)


def rules() -> ModelWorkStateRules:
    return ModelWorkStateRules.model_validate(
        yaml.safe_load(RULES_EXAMPLE.read_text(encoding="utf-8"))
    )

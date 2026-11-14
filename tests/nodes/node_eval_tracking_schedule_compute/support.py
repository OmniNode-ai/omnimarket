# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures of the weekly coding test's window trigger: a record, an overlay and an injected clock (OMN-24177)."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalCandidate,
)
from omnimarket.models.eval_tracking.model_eval_tracking_overlay import (
    ModelEvalTrackingOverlay,
)
from omnimarket.models.eval_tracking.model_eval_tracking_record import (
    sample_digest,
)

EPOCH = dt.datetime(2026, 11, 16, tzinfo=dt.UTC)
DAY = dt.timedelta(days=1)
TICK_MS = 1000
RESEND_SECONDS = 60
LOCAL = 0
GLM = 1


def candidates(n: int = 4) -> tuple[ModelCodeEvalCandidate, ...]:
    return tuple(
        ModelCodeEvalCandidate(
            repository="example/repo",
            merge_commit=f"{i + 1:040x}",
            contract_id=f"OMN-{1000 + i}",
        )
        for i in range(n)
    )


def record_document(**overrides: Any) -> dict[str, Any]:
    sample = candidates()
    document: dict[str, Any] = {
        "epoch": EPOCH,
        "seed": "seed-1",
        "band_target": 2,
        "tenant_id": "lab",
        "harness_revision": "rev-1",
        "generation_settings": {"temperature": "0"},
        "prompt_template": "template-v1",
        "candidates": sample,
        "task_sample_digest": sample_digest(sample),
        "arms": [
            {"model": "local-a", "backend_id": "lab-a", "cadence": "local"},
            {"model": "glm-x", "backend_id": "cloud-x", "cadence": "glm"},
        ],
    }
    document.update(overrides)
    return document


def overlay_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "local_days": 7,
        "glm_days": 28,
        "resend_seconds": RESEND_SECONDS,
        "record": record_document(),
    }
    document.update(overrides)
    return document


def make_overlay(**overrides: Any) -> ModelEvalTrackingOverlay:
    return ModelEvalTrackingOverlay.model_validate(overlay_document(**overrides))


def tick_at(at: dt.datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=0,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=TICK_MS,
    )


def turns(
    arm_index: int, start: dt.datetime, end: dt.datetime
) -> Iterator[dt.datetime]:
    """The tick times in [start, end) that are this arm's turn, on the injected clock."""
    arm_count = 2
    step = dt.timedelta(seconds=RESEND_SECONDS)
    first = EPOCH + step * (-(-(start - EPOCH) // step))
    at = first
    while at < end:
        if (int(at.timestamp()) // RESEND_SECONDS) % arm_count == arm_index:
            yield at
        at += step

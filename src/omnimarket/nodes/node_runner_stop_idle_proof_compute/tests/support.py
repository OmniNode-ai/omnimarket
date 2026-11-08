# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders of the runner-stop idle proof tests (OMN-22952): fixtures only, no node logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_runner_stop_idle_proof_compute.models import (
    ModelProofQueuedJob,
    ModelProofRead,
    ModelProofRunner,
    ModelRunnerStopIdleProofRequest,
    ModelRunnerStopIdleProofSettings,
)

FIRST_READ = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
GAP_SECONDS = 60
GROUP_A = 3
GROUP_B = 7
OTHER_GROUP = 4
STOP_LABEL = "stop-label-a"


def settings(**over: object) -> ModelRunnerStopIdleProofSettings:
    fields: dict[str, object] = {
        "stop_set_labels": (STOP_LABEL, "stop-label-b"),
        "proof_group_ids": (GROUP_A, GROUP_B),
        "required_reads": 2,
        "min_gap_seconds": GAP_SECONDS,
        "max_age_seconds": 300,
    }
    fields.update(over)
    return ModelRunnerStopIdleProofSettings.model_validate(fields)


def idle_read(offset_seconds: int, **over: object) -> ModelProofRead:
    fields: dict[str, object] = {
        "read_at": FIRST_READ + timedelta(seconds=offset_seconds),
        "groups_read": (GROUP_A, GROUP_B, OTHER_GROUP),
        "runners": (
            ModelProofRunner(name="r-a", group_id=GROUP_A, busy=False),
            ModelProofRunner(name="r-b", group_id=GROUP_B, busy=False),
            ModelProofRunner(name="r-other", group_id=OTHER_GROUP, busy=True),
        ),
        "queued_jobs": (ModelProofQueuedJob(job_id=1, labels=("unrelated",)),),
    }
    fields.update(over)
    return ModelProofRead.model_validate(fields)


def request(
    reads: tuple[ModelProofRead, ...] | None = None,
    *,
    now_offset_seconds: int = 90,
    **setting_over: object,
) -> ModelRunnerStopIdleProofRequest:
    return ModelRunnerStopIdleProofRequest(
        correlation_id="corr-stop-idle-proof",
        now=FIRST_READ + timedelta(seconds=now_offset_seconds),
        settings=settings(**setting_over),
        reads=(idle_read(0), idle_read(GAP_SECONDS)) if reads is None else reads,
    )

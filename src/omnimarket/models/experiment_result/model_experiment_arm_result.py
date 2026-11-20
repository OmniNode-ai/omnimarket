# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed event model for the result of one experiment arm."""

from __future__ import annotations

import uuid
from uuid import UUID, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelExperimentArmResult(BaseModel):
    """One scored arm of one experiment.

    Cross-field rules (correct <= n, interval consistent with the counts,
    p50 <= p90, engine policy) are NOT checked here but by
    node_experiment_result_record_compute. Every field is carried by the
    producer; nothing is read from a clock.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    experiment_id: str = Field(
        min_length=1, max_length=120, pattern=r"^[a-z0-9][a-z0-9._-]*$"
    )
    arm: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=200)
    engine: str = Field(min_length=1, max_length=40)
    n: int = Field(ge=1)
    correct: int = Field(ge=0)
    wilson_low: float = Field(ge=0.0, le=1.0)
    wilson_high: float = Field(ge=0.0, le=1.0)
    baseline_accuracy: float = Field(ge=0.0, le=1.0)
    latency_p50_ms: float = Field(ge=0.0)
    latency_p90_ms: float = Field(ge=0.0)
    gpu_seconds: float = Field(ge=0.0)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truth_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    measured_at: AwareDatetime
    report_ref: str = Field(default="", max_length=300)


class ModelExperimentArmResultRecorded(BaseModel):
    """The payload of the recorded terminal: one accepted arm result and its accuracy.

    node_experiment_result_record_compute publishes this on its success topic (its own
    output model carries the same fields and more, a status and the refusals), and
    node_projection_experiment_result consumes it. Both sides read the one shape here, so a change
    to either breaks the seam test. Unknown keys are ignored: the runtime adds envelope keys.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    result_id: UUID
    result: ModelExperimentArmResult
    accuracy: float = Field(ge=0.0, le=1.0)


def experiment_arm_result_id_for(result: ModelExperimentArmResult) -> UUID:
    """Deterministic id for one experiment arm result."""
    return uuid5(
        uuid.NAMESPACE_URL,
        f"omnimarket.experiment-arm-result|{result.tenant_id}|{result.experiment_id}|{result.arm}|{result.model_id}|{result.measured_at.isoformat()}",
    )


__all__ = [
    "ModelExperimentArmResult",
    "ModelExperimentArmResultRecorded",
    "experiment_arm_result_id_for",
]

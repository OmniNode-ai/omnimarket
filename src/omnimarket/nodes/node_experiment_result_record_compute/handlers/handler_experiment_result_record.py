# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check one experiment-arm result against the cross-field rules.

``handle`` takes a single experiment-arm result command, evaluates every
violation of the cross-field rules the wire model does not check, and returns
a typed completed or failed terminal. It reads no file, clock or network
except its policy, which comes from the packaged contract ``config:`` section
or a deployment overlay.
"""

from __future__ import annotations

import math

from omnimarket.models.experiment_result.model_experiment_arm_result import (
    experiment_arm_result_id_for,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.enum_experiment_result_record_status import (
    EnumExperimentResultRecordStatus,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.enum_experiment_result_refusal import (
    EnumExperimentResultRefusal,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_policy import (
    ModelExperimentResultPolicy,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_record_request import (
    ModelExperimentResultRecordRequest,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_record_result import (
    ModelExperimentResultRecordResult,
)
from omnimarket.nodes.node_experiment_result_record_compute.models.model_experiment_result_refusal import (
    ModelExperimentResultRefusal,
)
from omnimarket.nodes.node_experiment_result_record_compute.overlay import load_policy


def _wilson(correct: int, n: int, z: float) -> tuple[float, float]:
    """Two-sided Wilson interval for a proportion with a configurable z."""
    if not 0 <= correct <= n:
        raise ValueError("Wilson counts must satisfy 0 <= correct <= n")
    if n == 0:
        return 0.0, 1.0
    p = correct / n
    denominator = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denominator
    return max(0.0, center - half), min(1.0, center + half)


class HandlerExperimentResultRecord:
    """Check one experiment-arm result against the cross-field rules."""

    def __init__(self, policy: ModelExperimentResultPolicy | None = None) -> None:
        self._policy = policy if policy is not None else load_policy()

    def handle(
        self, request: ModelExperimentResultRecordRequest
    ) -> ModelExperimentResultRecordResult:
        result = request.result
        policy = self._policy
        refusals: list[ModelExperimentResultRefusal] = []

        # Rule 1: correct > n
        if result.correct > result.n:
            refusals.append(
                ModelExperimentResultRefusal(
                    refusal=EnumExperimentResultRefusal.CORRECT_EXCEEDS_N,
                    detail=f"correct={result.correct} exceeds n={result.n}",
                )
            )
        else:
            # Rule 2: Wilson interval mismatch
            wilson_low, wilson_high = _wilson(
                result.correct, result.n, policy.confidence_z
            )
            if (
                abs(wilson_low - result.wilson_low) > policy.interval_tolerance
                or abs(wilson_high - result.wilson_high) > policy.interval_tolerance
            ):
                refusals.append(
                    ModelExperimentResultRefusal(
                        refusal=EnumExperimentResultRefusal.INTERVAL_MISMATCH,
                        detail=(
                            f"carried low={result.wilson_low:.4f} high={result.wilson_high:.4f} "
                            f"recomputed low={wilson_low:.4f} high={wilson_high:.4f}"
                        ),
                    )
                )

        # Rule 3: latency order
        if result.latency_p50_ms > result.latency_p90_ms:
            refusals.append(
                ModelExperimentResultRefusal(
                    refusal=EnumExperimentResultRefusal.LATENCY_ORDER,
                    detail=(
                        f"latency_p50_ms={result.latency_p50_ms} "
                        f"exceeds latency_p90_ms={result.latency_p90_ms}"
                    ),
                )
            )

        # Rule 4: engine not allowed
        refused_lower = {e.lower() for e in policy.refused_engines}
        if result.engine.lower() in refused_lower:
            refusals.append(
                ModelExperimentResultRefusal(
                    refusal=EnumExperimentResultRefusal.ENGINE_NOT_ALLOWED,
                    detail=f"engine={result.engine} is not allowed",
                )
            )

        status = (
            EnumExperimentResultRecordStatus.FAILED
            if refusals
            else EnumExperimentResultRecordStatus.COMPLETED
        )
        accuracy = 0.0 if result.correct > result.n else result.correct / result.n

        return ModelExperimentResultRecordResult(
            status=status,
            result_id=experiment_arm_result_id_for(result),
            result=result,
            accuracy=accuracy,
            refusals=tuple(refusals),
        )

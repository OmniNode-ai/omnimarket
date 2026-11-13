# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLogRetention: the instant a proof's log may be removed, from the overlay's declared hours.

Definition-B compute: ``handle(request: ModelLogRetentionRequest) -> ModelLogRetentionResult``.
No I/O, no clock: the finish time is the request's.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.nodes.node_lab_proof_judge_compute.models.model_log_retention import (
    ModelLogRetentionRequest,
    ModelLogRetentionResult,
)
from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    ModelLabProofJudgeOverlay,
)


class HandlerLogRetention:
    """Keep the proof log for the overlay's ``retention.log_hours`` after the proof finished."""

    def __init__(self, overlay: ModelLabProofJudgeOverlay) -> None:
        self._overlay = overlay

    def handle(self, request: ModelLogRetentionRequest) -> ModelLogRetentionResult:
        hours = self._overlay.retention.log_hours
        return ModelLogRetentionResult(
            log_hours=hours, retain_until=request.finished_at + timedelta(hours=hours)
        )

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Archive and retain a final remote lane's host working directories."""

from pathlib import Path

from ..models.model_remote_lane_retention import (
    ModelRemoteLaneRetentionRequest,
    ModelRemoteLaneRetentionResult,
)
from ..run_retention import retain_run


class HandlerRemoteLaneRetention:
    def handle(
        self, request: ModelRemoteLaneRetentionRequest
    ) -> ModelRemoteLaneRetentionResult:
        return ModelRemoteLaneRetentionResult(
            **retain_run(
                Path(request.root),
                request.receipt,
                request.ledger,
                keep_days=request.keep_days,
                delete=request.delete,
            )
        )

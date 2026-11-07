# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compute the per-class threshold as the measured 24 hour peak and freeze it with a hash."""

from __future__ import annotations

import yaml

from omnimarket.models.model_andon_threshold import (
    ModelAndonThresholdFreeze,
    ModelThresholdComputeRequest,
    ModelThresholdComputeResult,
    freeze_hash,
)


class HandlerThresholdCompute:
    """A stop fires when a class's 24 hours exceed its measured peak, so the peak is the threshold."""

    def handle(
        self, request: ModelThresholdComputeRequest
    ) -> ModelThresholdComputeResult:
        thresholds = {
            item.class_id: max(item.window_counts) for item in request.classes
        }
        freeze = ModelAndonThresholdFreeze(
            window_hours=request.window_hours,
            thresholds=dict(sorted(thresholds.items())),
            threshold_hash=freeze_hash(request.window_hours, thresholds),
            recalibrated_on=request.recalibrated_on,
        )
        document = {
            "window_hours": freeze.window_hours,
            "thresholds": freeze.thresholds,
            "threshold_hash": freeze.threshold_hash,
            "recalibrated_on": freeze.recalibrated_on.isoformat(),
        }
        return ModelThresholdComputeResult(
            freeze=freeze, overlay_yaml=yaml.safe_dump(document, sort_keys=True)
        )

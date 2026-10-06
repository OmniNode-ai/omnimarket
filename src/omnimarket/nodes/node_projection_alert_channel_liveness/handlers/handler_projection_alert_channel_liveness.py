# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold: one completed probe becomes one verdict row.

No database, no bus, no clock. The status is the probe's measured fact; healthy
is derived from it so DEAD, NOT_CONFIGURED and PROBE_ERROR cannot accidentally
read as healthy. A throttled tick makes no claim about the channel and yields
an explicit skip rather than a fabricated row.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid5

from omnimarket.nodes.node_projection_alert_channel_liveness.models import (
    ModelAlertChannelLivenessProjectionRequest,
    ModelAlertChannelLivenessProjectionResult,
    ModelAlertChannelLivenessRow,
)

HANDLER_ID = "projection-alert-channel-liveness"

#: Fixed namespace for content identity on the bare definition-B adapter path.
#: Delivery facts are excluded, so identical results derive identical keys.
_CONTENT_KEY_NAMESPACE = UUID("9e3bf185-3e2a-5a69-a2c8-e1ccd01fc013")


def _content_key(request: ModelAlertChannelLivenessProjectionRequest) -> UUID:
    """Derive a stable key from the domain result alone."""
    canonical = json.dumps(
        request.result().model_dump(mode="json"), sort_keys=True, default=str
    )
    return uuid5(_CONTENT_KEY_NAMESPACE, canonical)


class HandlerProjectionAlertChannelLiveness:
    """Folds one liveness result into its durable verdict or a skip."""

    def handle(
        self, request: ModelAlertChannelLivenessProjectionRequest
    ) -> ModelAlertChannelLivenessProjectionResult:
        """Fold one result. Pure."""
        if not request.probed:
            return ModelAlertChannelLivenessProjectionResult(skipped=True)

        verdict = request.verdict
        # Wire validation already enforces this. Keep the guard explicit so
        # the fold never substitutes a status for an absent measurement.
        if verdict is None:
            raise ValueError("a probed liveness result must carry a verdict")

        correlation_id = (
            UUID(request.fallback_correlation_id)
            if request.fallback_correlation_id
            else _content_key(request)
        )
        row = ModelAlertChannelLivenessRow(
            correlation_id=correlation_id,
            status=verdict.status,
            healthy=verdict.status == "LIVE",
            reason=verdict.reason,
            slack_error=verdict.slack_error,
            probe_interval_seconds=request.probe_interval_seconds,
            failure_surfaced=request.failure_surfaced,
            checked_at=request.checked_at,
            source_topic=request.source_topic,
        )
        return ModelAlertChannelLivenessProjectionResult(row=row)


__all__ = ["HANDLER_ID", "HandlerProjectionAlertChannelLiveness"]

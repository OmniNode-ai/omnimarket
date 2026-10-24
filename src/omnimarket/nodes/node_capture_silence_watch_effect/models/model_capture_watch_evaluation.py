# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed terminal event returned from the def-B handler."""

from pydantic import AwareDatetime, BaseModel, ConfigDict

from omnimarket.nodes.node_capture_silence_watch_effect.models.model_capture_watch_delivery import (
    ModelCaptureWatchDelivery,
)
from omnimarket.nodes.node_capture_silence_watch_effect.models.model_capture_watch_verdict import (
    ModelCaptureWatchVerdict,
)


class ModelCaptureWatchEvaluation(BaseModel):
    """Every finding and attempted publication from this tick."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    evaluated_at: AwareDatetime
    verdicts: tuple[ModelCaptureWatchVerdict, ...]
    deliveries: tuple[ModelCaptureWatchDelivery, ...]
    heartbeat_published: bool
    heartbeat_error: str | None = None

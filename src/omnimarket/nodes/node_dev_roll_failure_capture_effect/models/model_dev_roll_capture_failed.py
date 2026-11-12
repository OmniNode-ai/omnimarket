# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed capture-failed result (OMN-24013, five whys R2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dev_roll_failure_capture_effect.models.enum_dev_roll_capture_failure import (
    EnumDevRollCaptureFailure,
)


class ModelDevRollCaptureFailed(BaseModel):
    """The terminal record or manifest could not be written; no terminal was recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str | None
    reason: EnumDevRollCaptureFailure
    detail: str

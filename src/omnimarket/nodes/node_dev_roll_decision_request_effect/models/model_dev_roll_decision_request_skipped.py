# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Why one queued dev roll did not yield a decision request."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dev_roll_decision_request_effect.models.enum_dev_roll_decision_request_skip_reason import (
    EnumDevRollDecisionRequestSkipReason,
)


class ModelDevRollDecisionRequestSkipped(BaseModel):
    """The typed skip of one manifest: no facts were published for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str | None
    reason: EnumDevRollDecisionRequestSkipReason
    detail: str | None

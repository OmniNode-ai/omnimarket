# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The measurement one help request leaves (OMN-26722, plan section 3.5, "Measured")."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneHelpReceipt(BaseModel):
    """Asking tier, answering tier, the backend and model that ran, configured effort and outcome.

    Effort is the effort the tier was configured with: the harness attempt record carries
    no effort, so it is recorded as configured, never as executed. A tier whose attempt
    record is missing names no executed backend or model (None, never the configured one).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    workflow_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    asking_tier: str = Field(min_length=1)
    answering_tier: str = Field(min_length=1)
    executed_backend_id: str | None = None
    executed_model: str | None = None
    configured_effort: str = Field(min_length=1)
    outcome: Literal[
        "help_cause_found", "help_retry", "help_stop", "help_failed", "refused"
    ]
    detail: str | None = None


__all__ = ["ModelLaneHelpReceipt"]

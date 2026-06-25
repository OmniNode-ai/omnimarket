# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Alert event for CLEAN-but-unarmed PRs [OMN-13322].

Emitted by node_merge_sweep_auto_merge_arm_effect when a PR that triage
classified CLEAN (the only condition under which an arm command is published)
could NOT have auto-merge armed. This closes the "controller never ran" vs
"ran and failed to arm" ambiguity from omnibase_core#1280: the completion
event proves the effect ran; this alert names the failed-to-arm case so it is
observable on a dedicated topic.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelAutoMergeUnarmedCleanAlertEvent(BaseModel):
    """Raised when a CLEAN PR could not be armed for auto-merge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str  # "owner/name"
    correlation_id: UUID
    run_id: UUID
    total_prs: int
    # Reason the arm failed (e.g. GraphQL error, missing OCC preflight per
    # OMN-10485). Always populated for an alert — an alert with no reason is a
    # contradiction, so this is required.
    reason: str
    elapsed_seconds: float = 0.0

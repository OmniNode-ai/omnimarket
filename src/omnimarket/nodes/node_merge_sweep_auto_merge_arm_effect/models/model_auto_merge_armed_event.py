# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completion event for node_merge_sweep_auto_merge_arm_effect [OMN-8960]."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelAutoMergeArmedEvent(BaseModel):
    """Emitted when auto-merge has been armed (or attempted) on a PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str  # "owner/name"
    correlation_id: UUID
    run_id: UUID
    total_prs: int
    armed: bool
    error: str | None = None  # set if armed=False
    elapsed_seconds: float = 0.0

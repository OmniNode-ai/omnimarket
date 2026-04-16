# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completion event for node_ci_rerun_effect [OMN-8962]."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelCiRerunTriggeredEvent(BaseModel):
    """Emitted when CI rerun has been triggered (or attempted) on a PR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str  # "owner/name"
    correlation_id: UUID
    run_id: UUID
    total_prs: int
    run_id_github: str  # The GitHub Actions run ID that was rerun
    rerun_triggered: bool
    error: str | None = None
    elapsed_seconds: float = 0.0

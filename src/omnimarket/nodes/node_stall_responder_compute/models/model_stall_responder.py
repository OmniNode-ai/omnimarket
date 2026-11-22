# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the stall responder (OMN-22885)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_stall_cause import (
    ModelCauseDispatch,
    ModelCauseSkip,
    ModelRowRefusal,
    ModelStallResponderRequest,
)


class ModelStallResponderResult(BaseModel):
    """Dispatches (one per distinct cause key), skips, the updated dispatched map and counts.

    refusals: stall rows whose stamp is not a calendar date (a shape-valid 2026-11-31T00:00:00Z);
    each is refused alone and the rest of the batch is answered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dispatches: tuple[ModelCauseDispatch, ...]
    skipped: tuple[ModelCauseSkip, ...]
    dispatched: dict[str, str]
    stall_rows: int
    unreadable_rows: int
    truncated_findings: int
    refusals: tuple[ModelRowRefusal, ...] = ()


__all__ = ["ModelStallResponderRequest", "ModelStallResponderResult"]

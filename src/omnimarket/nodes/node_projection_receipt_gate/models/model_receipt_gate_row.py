# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection models for receipt-gate dashboard snapshot."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelReceiptGateRow(BaseModel):
    """One receipt-gate check row for the dashboard projection snapshot.

    Mirrors the ``ReceiptGateRow`` TypeScript interface declared in
    ``omnidash/src/components/dashboard/receipt-gate/ReceiptGateWidget.tsx``.
    The dashboard widget consumes ``onex.snapshot.projection.receipt-gate.v1``
    and renders these rows directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Gate check name (e.g. 'ci_checks', 'pytest').")
    pass_: bool = Field(alias="pass", description="Whether this check passed.")
    detail: str = Field(default="", description="Human-readable check detail.")
    pr_ref: str | None = Field(
        default=None,
        description="PR reference string (e.g. 'OMN-12345 / #123').",
    )
    worker: str | None = Field(
        default=None,
        description="Identity of the node/agent that performed the check.",
    )
    verifier: str | None = Field(
        default=None,
        description="Independent verifier identity (must differ from worker).",
    )
    evidence_count: int | None = Field(
        default=None,
        ge=0,
        description="Number of evidence artifacts attached to the receipt.",
    )
    evidence_hash: str | None = Field(
        default=None,
        description="Content hash of the evidence bundle.",
    )
    signed_at: str | None = Field(
        default=None,
        description="ISO-8601 timestamp when the receipt was signed.",
    )
    observed_at: datetime = Field(
        description="When this event was projected (used for ordering).",
    )

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


__all__ = ["ModelReceiptGateRow"]

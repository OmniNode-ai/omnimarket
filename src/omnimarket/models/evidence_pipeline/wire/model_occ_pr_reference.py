# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT


"""ModelOccPrReference — created OCC PR metadata."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.evidence_pipeline.wire.types import EvidenceLifecycleState


class ModelOccPrReference(BaseModel):
    """Idempotent reference to a provisional or finalized OCC evidence PR."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    schema_version: Literal["1.0.0"] = "1.0.0"
    correlation_id: str = Field(..., min_length=1)
    validation_run_id: str = Field(..., min_length=1)
    ticket_id: str = Field(..., min_length=1)
    occ_repository: str = Field(..., min_length=1)
    pr_number: int = Field(..., ge=1)
    pr_url: str = Field(..., min_length=1)
    branch: str = Field(..., min_length=1)
    created_at: str = Field(..., min_length=1)
    writer_identity: str = Field(..., min_length=1)
    evidence_lifecycle_state: EvidenceLifecycleState = "PROVISIONAL"
    validation_result_hash: str | None = Field(default=None, min_length=1)
    commit_sha: str | None = Field(default=None, min_length=7)
    idempotency_key: str | None = Field(default=None, min_length=1)


__all__: list[str] = ["ModelOccPrReference"]

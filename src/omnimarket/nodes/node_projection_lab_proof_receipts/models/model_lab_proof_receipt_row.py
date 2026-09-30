# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of omninode_internal.lab_proof_receipts (OMN-19566)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from omnimarket.nodes.node_projection_lab_proof_receipts.models.enum_lab_proof_receipt_result import (
    EnumLabProofReceiptResult,
)


class ModelLabProofReceiptRow(BaseModel):
    """The latest receipt for one ``(repo, pr, head, profile, version)`` key.

    Re-running the same key replaces the row (plan section 3); the history
    stays on the topic as events. ``verifier_token`` is the verifier's answer
    at mint time against the proven head. It is a statement about the receipt,
    never a merge decision: the required ``lab-proof`` job re-verifies against
    the PR's live head.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    repo: str = Field(pattern=r"^OmniNode-ai/[A-Za-z0-9._-]+$")
    pr_number: int = Field(ge=1)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    profile_id: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    receipt_key: str = Field(min_length=1)
    handler_kind: str = Field(min_length=1)
    result: EnumLabProofReceiptResult
    verifier_token: str = Field(min_length=1)
    verifier_reason: str = Field(min_length=1)
    mandatory_checks: tuple[str, ...]
    missing_mandatory_checks: tuple[str, ...]
    failing_checks: tuple[str, ...]
    started_at: datetime
    finished_at: datetime
    runner_identity: str = Field(min_length=1)
    verifier_identity: str = Field(min_length=1)
    host: str = Field(min_length=1)
    slot: str = Field(min_length=1)
    carried_from: str = ""
    receipt: dict[str, JsonValue]


__all__ = ["ModelLabProofReceiptRow"]

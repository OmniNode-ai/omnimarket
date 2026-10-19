# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result models for the review finding identity compute (OMN-21190).

The input is the anchor-level view of a finding: where it sits and what it claims.
The recorded-finding envelope that carries these fields on the bus is a separate
ticket; this node depends only on the fields below, so the envelope can adopt them
without this node changing.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_review_finding_identity_compute.models.enum_anchor_status import (
    EnumAnchorLostReason,
    EnumAnchorStatus,
)


class ModelIdentityFinding(BaseModel):
    """One finding's anchor and claim, as a producer saw it on one snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_ref: str = Field(
        min_length=1,
        description="Caller's handle for the emission; echoed on the result.",
    )
    repository: str = Field(min_length=1)
    pr_number: int = Field(ge=1)
    snapshot_id: str = Field(
        min_length=1,
        description="Immutable snapshot the finding was made on; one hunk across heads is a different occurrence.",
    )
    path: str = Field(min_length=1)
    hunk_text: str = Field(
        description="The anchored source lines; blank lines and @@ headers are ignored."
    )
    category: str = Field(min_length=1)
    rule_id: str | None = Field(
        default=None, description="The rule id, where the finding has one."
    )
    claim_text: str = Field(min_length=1)
    claim_ordinal: int = Field(
        ge=0, description="The producer's ordinal of this claim within the hunk."
    )
    start_line: int | None = Field(
        default=None,
        ge=1,
        description="First anchored line in the snapshot's file, where known.",
    )


class ModelPriorOccurrence(BaseModel):
    """A finding already keyed on an earlier snapshot, with the lineage it belongs to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding: ModelIdentityFinding
    occurrence_key: str = Field(min_length=1)
    lineage_id: str = Field(min_length=1)


class ModelReanchorRequest(BaseModel):
    """Re-find a prior occurrence's hunk in a later snapshot's file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_ref: str = Field(min_length=1)
    prior: ModelPriorOccurrence
    target_snapshot_id: str = Field(min_length=1)
    target_path: str | None = Field(
        default=None,
        description="The file's path in the target snapshot when it was renamed.",
    )
    target_file_text: str | None = Field(
        default=None,
        description="The file in the target snapshot; None when it could not be read.",
    )


class ModelFindingIdentityInput(BaseModel):
    """Findings to key and prior occurrences to re-anchor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    new_findings: tuple[ModelIdentityFinding, ...] = ()
    reanchors: tuple[ModelReanchorRequest, ...] = ()
    min_similarity: float = Field(
        default=0.85,
        gt=0,
        le=1,
        description="Line-level similarity a lightly edited hunk must keep to be re-anchored.",
    )


class ModelResolvedIdentity(BaseModel):
    """One finding's identity after resolution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    finding_ref: str
    status: EnumAnchorStatus
    lineage_id: str
    occurrence_key: str | None = Field(
        description="None when the anchor is lost: no key is invented for a place that cannot be found."
    )
    prior_occurrence_key: str | None = None
    hunk_hash: str | None = None
    start_line: int | None = None
    lost_reason: EnumAnchorLostReason | None = None


class ModelAnchorCounts(BaseModel):
    """Per-status counts for the request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    new: int = Field(ge=0)
    unchanged: int = Field(ge=0)
    moved: int = Field(ge=0)
    anchor_lost: int = Field(ge=0)
    total: int = Field(ge=0)


class ModelFindingIdentityOutput(BaseModel):
    """Resolved identities, in request order (new findings, then re-anchors), with counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    identities: tuple[ModelResolvedIdentity, ...]
    counts: ModelAnchorCounts

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""What a ticket contract was accepted against: its pinned criterion hashes (OMN-20858)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCriterionPin(BaseModel):
    """One ``ac_bindings`` record: an item's binding of a label, pinned by hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(..., min_length=1, description="The dod_evidence item id.")
    label: str = Field(..., min_length=1, description="Canonical criterion label.")
    criterion_hash: str = Field(
        ...,
        min_length=1,
        description="sha256 of the criterion text the binding was accepted against.",
    )
    accepted: bool = Field(
        ...,
        description=(
            "True when a lane other than the binding's proposer accepted it "
            "(OMN-17427). Only an independently accepted pin re-accepts a "
            "criterion whose text moved."
        ),
    )


class ModelCriterionClaim(BaseModel):
    """One item's claim on one criterion label, through binds_ac or ac_bindings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(..., min_length=1, description="The dod_evidence item id.")
    label: str = Field(..., min_length=1, description="Canonical criterion label.")


class ModelCriteriaPins(BaseModel):
    """The criteria a ticket contract recorded, read from the contract alone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bindings: tuple[ModelCriterionPin, ...] = Field(
        default=(),
        description=(
            "Per-binding hashes, in contract order, without retired bindings "
            "and without records that carry no hash."
        ),
    )
    known_labels: tuple[str, ...] = Field(
        default=(),
        description=(
            "Every canonical label the contract claims: requirement acceptance "
            "ids, binding records and binds_ac entries."
        ),
    )
    declared_labels: tuple[str, ...] = Field(
        default=(),
        description=(
            "Labels the contract's requirements declare. The requirements block "
            "is not append-only, so a declaration is edited away, never retired."
        ),
    )
    claims: tuple[ModelCriterionClaim, ...] = Field(
        default=(),
        description="Every item claim on a label, before any retirement (OMN-18406).",
    )
    retired: tuple[ModelCriterionClaim, ...] = Field(
        default=(),
        description="Claims a supersedes_ac_binding entry withdrew outright.",
    )
    retired_if_absent: tuple[ModelCriterionClaim, ...] = Field(
        default=(),
        description=(
            "Independently accepted claims a no_longer_applicable entry withdraws "
            "only when the live ticket no longer carries the label."
        ),
    )


__all__ = ["ModelCriteriaPins", "ModelCriterionClaim", "ModelCriterionPin"]

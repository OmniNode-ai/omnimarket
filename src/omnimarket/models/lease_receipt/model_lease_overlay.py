# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lease overlay's typed shape: which kinds exist and what each admits.

Every kind, key pattern, admitted caller, ttl bound and receipt rule lives in
the overlay, never in code: the reducer reads only what a kind's entry here
names. A kind absent from ``lease_kinds`` is unknown to the node, and a key
the overlay omits refuses the load, so a deployment can only narrow what the
packaged contract ships.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.lease_receipt.enum_lease import (
    EnumLeaseOperation,
    EnumLeaseReleaseRule,
)


class ModelLeaseKindConfig(BaseModel):
    """One lease kind: its subjects, its rules and its admitted callers.

    ``key_pattern`` is the regex a subject must fully match to be leased in
    the kind. ``outcome_ref_pattern`` is the regex an outcome_ref must fully
    match; the empty string is always allowed whatever the pattern says. A
    kind that retains receipts bounds them to ``max_receipts_per_subject``
    per subject, oldest dropped; a kind that does not keeps a receipt only
    while its lease is live.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    key_pattern: str = Field(
        ...,
        description="The regex a subject must fully match to be leased in this kind.",
    )
    release_rule: EnumLeaseReleaseRule
    operations_enabled: tuple[EnumLeaseOperation, ...] = Field(..., min_length=1)
    max_ttl_seconds: int = Field(..., ge=1)
    admitted_callers: tuple[str, ...] = Field(..., min_length=1)
    retain_receipts: bool
    max_receipts_per_subject: int = Field(..., ge=1)
    max_now_skew_seconds: int = Field(
        ...,
        ge=0,
        description=(
            "How far a command's now may sit from the reducer's own clock; a "
            "command outside it is refused now-out-of-bounds."
        ),
    )
    outcome_ref_pattern: str = Field(
        ...,
        description=(
            "The regex an outcome_ref must fully match; the empty string is "
            "always allowed."
        ),
    )


class ModelLeaseOverlay(BaseModel):
    """The node's lease overlay: one config per lease kind, none in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lease_kinds: dict[str, ModelLeaseKindConfig] = Field(..., min_length=1)


__all__: list[str] = [
    "ModelLeaseKindConfig",
    "ModelLeaseOverlay",
]

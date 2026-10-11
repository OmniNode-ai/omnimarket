# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The drift between a live ticket's criteria and its contract, and the amendment it needs (OMN-20858)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_criteria_drift_kind import EnumCriteriaDriftKind


class ModelCriterionDrift(BaseModel):
    """One criterion on which the live ticket and the contract disagree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumCriteriaDriftKind = Field(...)
    label: str = Field(..., min_length=1, description="Canonical criterion label.")
    item_ids: tuple[str, ...] = Field(
        default=(), description="Contract items whose binding of this label moved."
    )
    pinned_hash: str | None = Field(
        default=None, description="The hash the binding was accepted against."
    )
    live_hash: str | None = Field(
        default=None, description="The hash of the live criterion text."
    )

    def summary(self) -> str:
        """One clause naming the criterion and what happened to it."""
        if self.kind is EnumCriteriaDriftKind.EDITED:
            return (
                f"{self.label} edited (items {', '.join(self.item_ids)} pinned "
                f"{(self.pinned_hash or '')[:12]}, live {(self.live_hash or '')[:12]})"
            )
        if self.kind is EnumCriteriaDriftKind.DELETED:
            return f"{self.label} deleted from the ticket"
        if self.kind is EnumCriteriaDriftKind.ADDED:
            return (
                f"{self.label} added to the ticket (live {(self.live_hash or '')[:12]})"
            )
        return f"{self.label} duplicate_label (two live criteria, no accepted pin)"


class ModelCriteriaAmendmentEntry(BaseModel):
    """One change the contract needs for one label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(..., min_length=1)
    item_ids: tuple[str, ...] = Field(default=())
    from_hash: str | None = Field(default=None)
    to_hash: str | None = Field(default=None)


class ModelCriteriaAmendment(BaseModel):
    """Every change the contract needs, as one record for one amendment.

    ``rebind`` entries move an existing binding to the live text, ``retire``
    entries withdraw bindings of a criterion the ticket dropped, ``add`` entries
    bind a criterion the contract never carried, and ``disambiguate`` entries
    name labels the ticket body must make unique first. A rebind is accepted by
    a lane other than the one that proposed it; until then the criterion stays
    unproven.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., min_length=1)
    criteria_revision: str = Field(
        ..., description="The live criteria revision this amendment was computed for."
    )
    rebind: tuple[ModelCriteriaAmendmentEntry, ...] = Field(default=())
    retire: tuple[ModelCriteriaAmendmentEntry, ...] = Field(default=())
    add: tuple[ModelCriteriaAmendmentEntry, ...] = Field(default=())
    disambiguate: tuple[ModelCriteriaAmendmentEntry, ...] = Field(default=())


class ModelCriteriaCheck(BaseModel):
    """What the criteria-drift check established for one verification run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criteria_revision: str | None = Field(
        default=None,
        description="Revision of the live criteria the verdict was computed against.",
    )
    drift: tuple[ModelCriterionDrift, ...] = Field(default=())
    amendment: ModelCriteriaAmendment | None = Field(default=None)
    unavailable_reason: str | None = Field(
        default=None,
        description="Set when the live ticket could not be read before or after the run.",
    )
    changed_during_verification: bool = Field(
        default=False,
        description="True when the criteria revision moved between the two reads.",
    )
    revision_after: str | None = Field(
        default=None, description="The revision read after the checks ran."
    )
    known_labels: tuple[str, ...] = Field(
        default=(), description="Labels the contract claims, in contract order."
    )

    @property
    def refused(self) -> bool:
        """True when the live criteria do not match what the contract accepted."""
        return bool(self.drift) or self.changed_during_verification

    @property
    def invalidated_labels(self) -> frozenset[str]:
        """Labels whose prior PASS evidence no longer counts."""
        if self.changed_during_verification:
            return frozenset(self.known_labels) | frozenset(d.label for d in self.drift)
        return frozenset(d.label for d in self.drift)

    def message(self, ticket_id: str) -> str:
        """The ``CRITERIA_DRIFT`` refusal text, naming every criterion."""
        clauses = [d.summary() for d in self.drift]
        if self.changed_during_verification:
            clauses.append(
                "the ticket's criteria changed while this verification ran "
                f"(revision {(self.criteria_revision or '')[:12]} -> "
                f"{(self.revision_after or '')[:12]})"
            )
        return (
            f"CRITERIA_DRIFT: the live criteria of {ticket_id} are not the ones its "
            f"contract was accepted against: {'; '.join(clauses)}. A changed, "
            "deleted, added or ambiguous criterion leaves its binding unproven and "
            "its earlier PASS evidence no longer counts until a second lane "
            "re-accepts it; the amendment the contract needs is on the verdict."
        )


__all__ = [
    "ModelCriteriaAmendment",
    "ModelCriteriaAmendmentEntry",
    "ModelCriteriaCheck",
    "ModelCriterionDrift",
]

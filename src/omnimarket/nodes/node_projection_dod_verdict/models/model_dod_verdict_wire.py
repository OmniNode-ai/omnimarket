# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire shape of one ``onex.evt.omnimarket.dod-verify-completed.v1`` event.

A LOCAL wire model, deliberately not an import of ``node_dod_verify``'s
``ModelDodVerifyCompletedEvent``. Three reasons, all load-bearing:

* a sibling node may never reach into another node's models package
  (OMN-9263). ``EnumDodVerifyStatus`` and ``EnumDodVerifyUnresolvedCause``
  live in the shared ``omnimarket.enums`` package for exactly that reason and
  ARE imported here; the model does not and is restated.
* the producing model is ``extra="forbid"``. A reducer that refuses an event
  carrying a field the producer added last week converts a schema addition
  into silent data loss on the consuming side, so this model ignores unknown
  fields instead.
* the two counters added after the captured 2026-09-20 payload was written --
  ``readback_proving_count`` (OMN-18135) and ``unbindable_overlay_count``
  (OMN-17323) -- default to zero here as they do on the producer. An older
  payload therefore projects with zeros in those columns rather than failing,
  and the zero is indistinguishable from a real zero. That is stated rather
  than hidden: the row carries ``total_checks`` beside them, so a reader can
  see the counters do not account for the total.

``checks`` is accepted and DISCARDED. The per-check records are already on the
producer's terminal payload and re-materialising them here would make this
table grow with the check population rather than with the run population,
which is the shape the plan's metric reads. What the metric needs is the class
counts, and they arrive as counts.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_dod_verify_status import EnumDodVerifyStatus
from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)


class ModelDodVerdictWire(BaseModel):
    """One completed definition-of-done verification as it arrives off the bus."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    correlation_id: UUID = Field(
        ...,
        description=(
            "The verification run's correlation id. Typed as a UUID on both "
            "sides of this topic, and half the row's key."
        ),
    )
    ticket_id: str = Field(
        ...,
        min_length=1,
        description=(
            "The Linear ticket the verification is about. A bare string on "
            "the producer, and the standing identifier rule exempts it by "
            "name because it is not a UUID -- but it is unvalidated there, "
            "so this projection is the first surface that would see a "
            "malformed value. Length is asserted; the shape is not guessed at."
        ),
    )
    status: EnumDodVerifyStatus = Field(
        ..., description="Terminal status of the verification run."
    )
    unresolved_cause: EnumDodVerifyUnresolvedCause | None = Field(
        default=None,
        description="Why the run reached no verdict. Set only when unresolved.",
    )
    # OMN-19514: the delegation run the verification judged, when the caller
    # named one. None on every payload written before the field existed and on
    # every verification that judged no delegated attempt.
    delegation_correlation_id: UUID | None = Field(
        default=None,
        description=(
            "Correlation id of the delegation run this verification judged; "
            "the delegation_events key it joins on."
        ),
    )

    started_at: datetime = Field(..., description="When the run started.")
    completed_at: datetime = Field(
        ...,
        description=(
            "When the run finished. Event time, never an ingest clock, and "
            "the third component of the row key so a re-verification of the "
            "same ticket is a new row rather than an overwrite of the old "
            "verdict. Attempts-until-done is a question about history."
        ),
    )

    total_checks: int = Field(
        default=0, ge=0, description="Verdict-bearing checks run."
    )
    verified_count: int = Field(default=0, ge=0, description="Checks that passed.")
    failed_count: int = Field(default=0, ge=0, description="Checks that failed.")
    skipped_count: int = Field(default=0, ge=0, description="Checks not executed.")
    superseded_count: int = Field(
        default=0, ge=0, description="Checks a later evidence item superseded."
    )
    non_probative_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Checks that ran and exited zero while proving nothing about this "
            "ticket. Carried because the plan's report prints the "
            "non-probative share beside the improvement it would produce, so "
            "a definition of done thinning out is visible."
        ),
    )
    behavior_proving_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Checks that both passed AND executed the claimed behaviour. The "
            "conjunct the eval metric's done predicate requires."
        ),
    )
    readback_proving_count: int = Field(
        default=0,
        ge=0,
        description=(
            "Checks that read live state and asserted on it. Counted "
            "alongside the behaviour-proving count, never added into it."
        ),
    )
    unbindable_overlay_count: int = Field(
        default=0, ge=0, description="Synthetic overlay items excluded from the total."
    )

    error_message: str | None = Field(
        default=None, description="Failure detail, when the run carries one."
    )

    # OMN-18901. A rehearsal, not a verdict.
    #
    # The producing node accepts a dry-run flag whose documented meaning is
    # "run the checks and emit nothing". Nothing emitted at all until the
    # producer was wired, so the flag had never had to mean anything; now that
    # the run publishes, it does.
    #
    # The refusal lives HERE, on the consuming side, rather than as a
    # suppressed publish upstream, for a reason that is structural rather than
    # stylistic: the runtime publishes a definition-B handler's returned model
    # automatically, so the producer has no seam at which to withhold one
    # message and still return its verdict. What the projection CAN do is
    # decline to make a rehearsal durable, and deciding what is worth storing
    # is the projection's job in the first place.
    #
    # Defaulting to False means every payload written before this field
    # existed, and every producer that never sets it, projects exactly as it
    # did — the flag can only ever withhold a row that was explicitly marked a
    # rehearsal, never silently drop a real one.
    dry_run: bool = Field(
        default=False,
        description=(
            "Whether the producing run was a rehearsal. A true value is not "
            "projected: attempts-until-done counts real attempts."
        ),
    )


__all__ = ["ModelDodVerdictWire"]

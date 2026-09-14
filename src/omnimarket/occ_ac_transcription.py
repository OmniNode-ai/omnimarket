# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transcribe an author's criterion-to-falsifier map into companion bindings.

OMN-18332 (step 6 of the mechanical closeout plan). This module contributes no
judgement whatsoever. It does not read a diff, it does not match a check name
against a criterion, and it never decides that a criterion is satisfied. It
copies a declaration the ticket's author already wrote, and records who wrote it
and when.

The distinction OMN-18238 drew is the one this preserves: a machine may
PROPOSE a binding, a person ACCEPTS it. Here the person has already accepted --
by writing the criterion and the check that would settle it in one act, at
creation time, before any evidence existed -- so the acceptance is transcribed
rather than invented. ``proposed_by`` names the transcriber; ``accepted_by``
names the author. Both fields are in the artifact and they never hold the same
value.

Why acceptance is granted ONLY to the creation revision
------------------------------------------------------
Attributing acceptance to whoever last edited the criterion is accurate and
still worthless, because it closes the ticket in the sequence this mechanism
exists to refuse:

    declare criterion with falsifier F1 -> run F1 -> watch it fail -> edit the
    criterion to name F2, a check that already passes -> mint the companion

Every field in the resulting record would be true. The property the whole
mechanism rests on is not attribution, it is that **the declaration precedes
the outcome**, and only the creation revision carries that. So a criterion whose
text has moved since creation yields a DRAFT, and re-acceptance is a deliberate
human act on the superseding-evidence path -- never something re-minting grants.

What "moved since creation" means is settled per criterion, not per
description, because the creation revision's own text is readable (see
:mod:`omnimarket.occ_creation_revision`). An edit to criterion B leaves
criterion A's acceptance standing.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.occ_contract_pin import contract_pin_hashes
from omnimarket.occ_creation_revision import ModelCreationRevision
from omnimarket.occ_criterion_normalizer import (
    markdown_comparison_text,
    rich_text_comparison_text,
)
from omnimarket.occ_criterion_units import (
    DEFAULT_CRITERION_POLICY,
    CriterionPolicy,
    criterion_units,
)

__all__ = [
    "AUTOBINDER_IDENTITY",
    "ModelTranscribedBinding",
    "transcribe_ac_bindings",
]

#: What goes in ``proposed_by``. It names a MACHINE on purpose: a reader looking
#: at an accepted binding can see that the acceptance was transcribed by the
#: autobinder from the author's own words, rather than typed by the author into
#: the contract. Matches the branch prefix the companion is minted on.
AUTOBINDER_IDENTITY = "occ-autobind"


class ModelTranscribedBinding(BaseModel):
    """One acceptance criterion's binding record, accepted or draft.

    ``accepted_by`` and ``accepted_at`` are populated together or not at all --
    :meth:`is_accepted` is the only predicate any consumer should use, and the
    model refuses a half-populated acceptance at construction so a record that
    reads as accepted to one consumer and draft to another cannot exist.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(..., description="The criterion label, e.g. 'AC1'.")
    criterion_hash: str = Field(
        ...,
        description=(
            "sha256 of the LIVE criterion text as the consumer reads it -- the "
            "digest `onex_change_control`'s `ac_binding_stale_hash` rule will "
            "recompute from the ticket body. Never the comparison projection's "
            "digest: the projection answers whether the text has MOVED, and its "
            "digest matches nothing the gate computes."
        ),
    )
    falsifier: str = Field(
        ...,
        description="The check the author declared would settle this criterion.",
    )
    proposed_by: str = Field(
        ..., description="What transcribed the binding. Never a person."
    )
    accepted_by: str | None = Field(
        default=None,
        description=(
            "The actor who wrote the criterion at the ticket's creation "
            "revision. None on a draft."
        ),
    )
    accepted_at: datetime | None = Field(
        default=None,
        description=(
            "The ticket's creation timestamp -- never the mint time. None on a draft."
        ),
    )

    def model_post_init(self, __context: object) -> None:
        if (self.accepted_by is None) != (self.accepted_at is None):
            msg = (
                "an acceptance is an actor AND a time; a record carrying one "
                f"without the other is neither accepted nor draft (label "
                f"{self.label!r})"
            )
            raise ValueError(msg)
        if self.accepted_by is not None and self.accepted_by == self.proposed_by:
            msg = (
                "the transcriber may never appear as the acceptor; that is the "
                "whole distinction the record exists to carry (label "
                f"{self.label!r})"
            )
            raise ValueError(msg)

    @property
    def is_accepted(self) -> bool:
        """True iff this record carries a full acceptance."""
        return self.accepted_by is not None and self.accepted_at is not None


def transcribe_ac_bindings(
    *,
    live_description: str,
    creation_revision: ModelCreationRevision | None,
    created_at: datetime,
    creator_id: str | None = None,
    proposed_by: str = AUTOBINDER_IDENTITY,
    policy: CriterionPolicy = DEFAULT_CRITERION_POLICY,
) -> tuple[ModelTranscribedBinding, ...]:
    """Every declared criterion's binding, accepted where the text has not moved.

    Args:
        live_description: the issue's current markdown description.
        creation_revision: the revision whose snapshot time equals the ticket's
            creation time, or ``None`` when it could not be resolved.
        created_at: the ticket's ``createdAt`` -- the acceptance timestamp.
        creator_id: the issue's creator, used as the acceptor only when the
            creation revision itself records no actor. It is the same fact from
            a second field, not a guess: at the creation revision the creator IS
            the actor.
        proposed_by: the transcriber's identity.
        policy: the criterion vocabulary, vendored from the admission guard.

    Returns:
        One record per criterion that carries BOTH a label and a declared
        falsifier, ordered as the description lists them. A criterion with no
        falsifier yields nothing at all -- that is the whole of AC5, and the
        ticket then holds exactly as it does today. A criterion with no label is
        likewise skipped: a binding needs something stable to point at, and a
        position-derived ordinal renumbers every binding below it the moment a
        bullet is inserted.

        When ``creation_revision`` is ``None`` every returned record is a DRAFT.
        Absence of the creation revision is never read as agreement with the
        current text (AC2g).

        A criterion whose label the CONSUMER's reader does not resolve in this
        body yields nothing either, for the same fail-closed reason: a pin the
        gate has nothing to match against is a refusal, not evidence.
    """
    live_units = criterion_units(markdown_comparison_text(live_description), policy)
    # What the CONSUMER will recompute, per label, from the raw markdown. The
    # projection's digests below decide only whether a criterion has moved since
    # creation; they are not comparable with anything the gate computes, and
    # pinning them is what made every entry of OCC#9486 read as stale.
    pins = contract_pin_hashes(live_description)

    creation_by_label: dict[str, str] = {}
    acceptor: str | None = None
    if creation_revision is not None:
        creation_units = criterion_units(
            rich_text_comparison_text(creation_revision.content_data), policy
        )
        for unit in creation_units:
            # A duplicate label at creation is ambiguous about which text the
            # author accepted, so the FIRST wins and the rest are ignored --
            # ambiguity resolves toward fewer acceptances, never more.
            if unit.label is not None and unit.label not in creation_by_label:
                creation_by_label[unit.label] = unit.criterion_hash
        acceptor = creation_revision.actor_id or (creator_id or None)

    records: list[ModelTranscribedBinding] = []
    for unit in live_units:
        if unit.label is None or unit.falsifier is None:
            continue
        # Fail closed on a label the consumer's reader does not resolve in this
        # body -- a zero-padded ordinal is the measured case, read as `AC01`
        # here and `AC1` there. A binding minted under a label the gate cannot
        # find is an `ac_binding_unknown_criterion` refusal, so none is minted
        # and the coverage rule holds the criterion instead.
        pinned = pins.get(unit.label)
        if pinned is None:
            continue
        unchanged = creation_by_label.get(unit.label) == unit.criterion_hash
        # A transcriber may never name itself as the acceptor. If the only
        # actor available is the transcriber's own identity, that is not an
        # author's declaration and the record stays a draft.
        may_accept = (
            unchanged and acceptor is not None and acceptor.strip() != proposed_by
        )
        records.append(
            ModelTranscribedBinding(
                label=unit.label,
                criterion_hash=pinned,
                falsifier=unit.falsifier,
                proposed_by=proposed_by,
                accepted_by=acceptor if may_accept else None,
                accepted_at=created_at if may_accept else None,
            )
        )
    return tuple(records)

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Models for node_projection_open_obligations -- the open-obligations surface.

[OMN-17019] C9 of the OMN-16176 ledger ladder. An **obligation** is what one
actor owes another. It is created, transferred, satisfied, superseded or
abandoned by a TYPED EVENT against a contract -- never by editing a markdown
file. ``session-goal.md``, ``ROLLING_SEVEN_DAY_PLAN.md``, the rolling ledger's
open-ask section and the handoff are all **renderers** over the projection this
module feeds; none of them is the authority.

WHY THE KIND ENUM IS DECLARED HERE AND NOT IMPORTED FROM omnibase_core
    omnibase_core carries the five OMN-16177 work-event kinds
    (``omnibase_core.enums.enum_work_event_kind``) from core 0.47.x, but this
    repository pins ``omnibase-core>=0.46.13,<0.47.0`` -- those symbols are not
    importable here at the pin that actually resolves. The sibling
    ``node_projection_work_events`` (OMN-16180) hit the identical wall and set
    the precedent of a node-local kind enum; this node follows it rather than
    forcing a core release onto this ticket's critical path. The wire values
    below are the registry ``event_type`` keys verbatim, so the two surfaces
    reconcile by string when the pin moves.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bounded narrative width, per OMN-16177's ModelWorkEventBase.summary contract.
MAX_SUMMARY_CHARS = 2000

# Bound on the free-text evidence/reason fields. An obligation closes on a
# delivered artifact reference, not on an essay (off-rails rev 2, A14).
MAX_REFERENCE_CHARS = 512


class EnumObligationEventKind(StrEnum):
    """The five obligation lifecycle kinds.

    Values are the emit-registry ``event_type`` keys verbatim
    (``node_emit_daemon/registries/topics.yaml``), so
    ``EnumObligationEventKind.CREATED.value`` is exactly the key the daemon
    routes on. They sit in the ``work.`` namespace alongside OMN-16177's
    ``work.claim.*`` / ``work.result.*`` kinds because an obligation is a work
    event, not a new event family -- there is no obligation topic family and no
    obligation-specific transport.
    """

    CREATED = "work.obligation.created"
    """An actor takes on -- or is handed -- something owed. Opens the record."""

    TRANSFERRED = "work.obligation.transferred"
    """The debt moves to a different owner. The obligation itself survives."""

    SATISFIED = "work.obligation.satisfied"
    """Closed by delivery: an artifact reference plus a delivery state."""

    SUPERSEDED = "work.obligation.superseded"
    """Closed because a different obligation replaced it. Names its successor."""

    ABANDONED = "work.obligation.abandoned"
    """Closed without delivery, with a recorded reason. Never a silent drop."""


TERMINAL_KINDS: frozenset[EnumObligationEventKind] = frozenset(
    {
        EnumObligationEventKind.SATISFIED,
        EnumObligationEventKind.SUPERSEDED,
        EnumObligationEventKind.ABANDONED,
    }
)
"""Kinds that close an obligation.

These are the ONLY kinds permitted to write ``closed_state``. That restriction
is what makes the fold safe against a consumer restarting from an earlier
partition offset: a re-delivered ``created`` cannot reopen a closed obligation,
because it never touches the column the state is derived from.
"""


class EnumObligationState(StrEnum):
    """The derived lifecycle state of an obligation.

    NEVER written by this node. The ``open_obligations.state`` column is a
    Postgres ``GENERATED ALWAYS ... STORED`` column over ``closed_state``, so
    "what is currently owed" is a *derivation* of the recorded facts and cannot
    be set to disagree with them -- not by this handler, not by a replay, and
    not by a hand-run UPDATE.
    """

    OPEN = "open"
    SATISFIED = "satisfied"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


CLOSED_STATE_BY_KIND: dict[EnumObligationEventKind, EnumObligationState] = {
    EnumObligationEventKind.SATISFIED: EnumObligationState.SATISFIED,
    EnumObligationEventKind.SUPERSEDED: EnumObligationState.SUPERSEDED,
    EnumObligationEventKind.ABANDONED: EnumObligationState.ABANDONED,
}
"""Terminal kind -> the ``closed_state`` it records. Total over TERMINAL_KINDS."""


class EnumActorKind(StrEnum):
    """``ModelActor`` discriminator (OMN-16177): who recorded the event."""

    SESSION = "session"
    NODE = "node"


class ObligationProjectionError(ValueError):
    """A wire event cannot be projected, and must not silently vanish.

    Raised rather than returning a degraded row, so the runtime's dispatch seam
    DLQs the message and the failure is observable. Off-rails rev 2 makes this
    the required failure behaviour for this surface specifically: obligation
    *reads* may degrade to the last-good projection and say so, but obligation
    *writes* FAIL CLOSED -- an obligation that cannot be recorded must block the
    close, never be dropped.
    """


class ModelObligationEventInbound(BaseModel):
    """One event off any of the five contract-declared obligation topics.

    Heterogeneous by topic, so per-kind fields are optional on the model and
    enforced per-kind by the handler against ``REQUIRED_FIELDS_BY_KIND`` -- the
    same split ``ModelWorkEventInbound`` uses. ``extra="ignore"`` so an additive
    field on an upstream emitter does not break the projection.

    ``emitted_at`` is REQUIRED and has no default: a projection must never
    invent an event time it was not given (OMN-16177 acceptance 5).
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    obligation_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Stable identity of the thing owed, across its whole lifecycle. "
            "Also the partition key: every event about one obligation lands in "
            "one partition, so the fold resolves by partition offset."
        ),
    )
    emitted_at: datetime = Field(
        ...,
        description=(
            "Emitter-assigned event time. DISPLAY SORT ONLY -- ordering comes "
            "from the partition offset, never from this field."
        ),
    )
    actor_id: str = Field(
        ...,
        min_length=1,
        description="Who recorded the event: session handle or node id.",
    )
    actor_kind: EnumActorKind = Field(
        default=EnumActorKind.SESSION,
        description="ModelActor discriminator for the recording actor.",
    )
    summary: str = Field(
        ...,
        min_length=1,
        max_length=MAX_SUMMARY_CHARS,
        description="Bounded narrative. Structured fields carry the evidence.",
    )

    # --- work.obligation.created -------------------------------------------
    asked_by: str | None = Field(
        default=None, max_length=MAX_REFERENCE_CHARS, description="Who asked for it."
    )
    owed_by: str | None = Field(
        default=None,
        max_length=MAX_REFERENCE_CHARS,
        description="Who owes it. Set on created, and re-set on transferred.",
    )
    acceptance_condition: str | None = Field(
        default=None,
        max_length=MAX_SUMMARY_CHARS,
        description="What would make this satisfied. Recorded at creation time.",
    )
    ticket_id: str | None = Field(
        default=None,
        max_length=64,
        description="Ticket this obligation concerns, if any.",
    )

    # --- work.obligation.satisfied -----------------------------------------
    evidence_uri: str | None = Field(
        default=None,
        max_length=MAX_REFERENCE_CHARS,
        description=(
            "Delivered artifact reference. Off-rails A14: an obligation closes "
            "on a delivered artifact plus a delivery state, NOT on a ticket id."
        ),
    )
    delivery_state: str | None = Field(
        default=None,
        max_length=64,
        description="How the artifact was delivered (e.g. sent, published, merged).",
    )

    # --- work.obligation.superseded ----------------------------------------
    superseded_by_obligation_id: str | None = Field(
        default=None,
        max_length=128,
        description="The obligation that replaced this one. Never a free-text note.",
    )

    # --- work.obligation.abandoned -----------------------------------------
    abandon_reason: str | None = Field(
        default=None,
        max_length=MAX_REFERENCE_CHARS,
        description="Why it was dropped. Recorded so a drop is never silent.",
    )

    @field_validator(
        "obligation_id",
        "actor_id",
        "summary",
        "asked_by",
        "owed_by",
        "acceptance_condition",
        "ticket_id",
        "evidence_uri",
        "delivery_state",
        "superseded_by_obligation_id",
        "abandon_reason",
    )
    @classmethod
    def _reject_blank(cls, value: str | None) -> str | None:
        """A whitespace-only value is absence wearing a present-looking mask."""
        if value is not None and not value.strip():
            raise ValueError("value must not be blank or whitespace-only")
        return value


REQUIRED_FIELDS_BY_KIND: dict[EnumObligationEventKind, tuple[str, ...]] = {
    EnumObligationEventKind.CREATED: (
        "asked_by",
        "owed_by",
        "acceptance_condition",
    ),
    EnumObligationEventKind.TRANSFERRED: ("owed_by",),
    EnumObligationEventKind.SATISFIED: ("evidence_uri", "delivery_state"),
    EnumObligationEventKind.SUPERSEDED: ("superseded_by_obligation_id",),
    EnumObligationEventKind.ABANDONED: ("abandon_reason",),
}
"""Per-kind mandatory payload fields, enforced by the handler, not by the model.

These MUST stay identical to the ``required_fields`` the emit registry declares
for the matching ``event_type`` -- ``tests/unit/nodes/
node_projection_open_obligations/test_handler_projection_open_obligations.py``
asserts that equality against ``topics.yaml`` directly, so the two cannot drift.

There is no default for any of them. A ``created`` without an acceptance
condition is an obligation nobody can ever prove satisfied, and a ``satisfied``
without an artifact reference is the exact "declared done, never delivered"
failure this ticket was filed against.
"""


class ModelOpenObligationRow(BaseModel):
    """One row written to ``omninode_internal.open_obligations``.

    Column semantics are documented once, authoritatively, in the migration
    (``migrations/0001_create_open_obligations.sql`` section 4). Every field
    here is optional-with-``None`` because a single event writes only the
    columns ITS KIND OWNS -- the adapters' UPSERT is a targeted-column merge
    (``ON CONFLICT ... DO UPDATE SET`` naming only the incoming columns), so
    columns this event does not name survive untouched.
    """

    model_config = ConfigDict(frozen=True)

    obligation_id: str = Field(..., min_length=1)
    last_event_kind: str = Field(..., min_length=1)
    last_event_at: datetime
    actor_kind: EnumActorKind
    actor_id: str = Field(..., min_length=1)
    source_topic: str = Field(..., min_length=1)
    payload: dict[str, object] = Field(default_factory=dict)

    created_at: datetime | None = Field(default=None)
    asked_by: str | None = Field(default=None)
    original_owed_by: str | None = Field(default=None)
    acceptance_condition: str | None = Field(default=None)
    opened_summary: str | None = Field(default=None)
    ticket_id: str | None = Field(default=None)

    transferred_owed_by: str | None = Field(default=None)

    closed_state: EnumObligationState | None = Field(default=None)
    closed_at: datetime | None = Field(default=None)
    evidence_uri: str | None = Field(default=None)
    delivery_state: str | None = Field(default=None)
    superseded_by: str | None = Field(default=None)
    abandon_reason: str | None = Field(default=None)


class ModelProjectionOpenObligationsResult(BaseModel):
    """Output of one projection operation."""

    model_config = ConfigDict(frozen=True)

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default="open_obligations")


__all__: list[str] = [
    "CLOSED_STATE_BY_KIND",
    "MAX_REFERENCE_CHARS",
    "MAX_SUMMARY_CHARS",
    "REQUIRED_FIELDS_BY_KIND",
    "TERMINAL_KINDS",
    "EnumActorKind",
    "EnumObligationEventKind",
    "EnumObligationState",
    "ModelObligationEventInbound",
    "ModelOpenObligationRow",
    "ModelProjectionOpenObligationsResult",
    "ObligationProjectionError",
]

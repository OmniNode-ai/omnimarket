# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Models for node_projection_work_events -- the L1 work-ledger surface.

[OMN-16180] C4 of the OMN-16176 ledger ladder. The kinds these models project
are the omniclaude session/hook events that are ALREADY flowing; the C1
(OMN-16177) ``work.claim.*`` / ``work.result.*`` kinds land on the same table
through the same handler once the C2 emit path publishes them -- see
``EnumWorkEventKind`` for why the surface is kind-agnostic.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bounded narrative width, per OMN-16177's ModelWorkEventBase.summary contract.
MAX_SUMMARY_CHARS = 2000


class EnumWorkEventKind(StrEnum):
    """Work-event kinds this projection materializes today.

    These are the SESSION-ACTIVITY kinds derived from the four live omniclaude
    hook topics. They are deliberately NOT the five OMN-16177 C1 kinds
    (``work.claim.requested``, ``work.claim.released``, ``work.result.recorded``,
    ``work.ruling.recorded``, ``work.correction.recorded``) -- those are emitted
    by the C2 emit path, which does not exist yet, and claiming them here would
    assert a capability the pipeline does not have.

    The ``work_events`` table itself is kind-agnostic (``event_kind`` is TEXT,
    not an enum-constrained column), so when C2 lands, its kinds project onto
    the same surface with no migration and no shape change.
    """

    SESSION_STARTED = "session.started"
    SESSION_PROMPT = "session.prompt"
    SESSION_TOOL = "session.tool"
    SESSION_ENDED = "session.ended"


class EnumActorKind(StrEnum):
    """``ModelActor`` discriminator (OMN-16177).

    Every row this node writes today is ``SESSION`` -- the omniclaude hooks are
    session actors. ``NODE`` exists so a node actor is representable on this
    surface without a migration when C8 (OMN-16190) converges node workflows
    onto the same ledger.
    """

    SESSION = "session"
    NODE = "node"


class WorkEventProjectionError(ValueError):
    """A wire event cannot be projected, and must not silently vanish.

    Raised rather than returning a degraded row, so the runtime's dispatch seam
    DLQs the message and the failure is observable -- deterministic-truth
    doctrine section 9, and OMN-16180 acceptance 6. Silently substituting a
    default (a ``now()`` timestamp, an empty actor) is exactly the failure class
    OMN-16994 was filed for: a projection that reports success while throwing
    the record away.
    """


class ModelWorkEventInbound(BaseModel):
    """One event off any of the four contract-declared subscribe topics.

    Optional-by-default because a single model receives heterogeneous topics
    (the same approach ``ModelSessionReplayEvent`` takes). ``extra="ignore"`` so
    an additive field on an upstream emitter does not break the projection.

    ``emitted_at`` is REQUIRED and has no default. The four hook emitters all
    stamp it (``node_event_emit_effect``, live-verified on the wire
    2026-08-29), and a projection must never invent an event time it was not
    given -- OMN-16177 acceptance 5 forbids a ``datetime.now()`` default on the
    emitter side, and inventing one here would launder the same defect one hop
    downstream.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str = Field(..., min_length=1, description="Session identifier.")
    emitted_at: datetime = Field(
        ..., description="Emitter-assigned event time. Display sort only."
    )

    working_directory: str | None = Field(default=None)
    hook_source: str | None = Field(default=None)
    correlation_id: str | None = Field(default=None)

    # prompt-submitted
    prompt_length: int | None = Field(default=None, ge=0)

    # tool-executed
    tool_name: str | None = Field(default=None)
    duration_ms: int | None = Field(default=None, ge=0)
    interrupted: bool | None = Field(default=None)

    # session-ended
    reason: str | None = Field(default=None)

    @field_validator("session_id")
    @classmethod
    def _session_id_is_not_blank(cls, value: str) -> str:
        """A whitespace-only session id is an unusable actor identity."""
        if not value.strip():
            raise ValueError("session_id must not be blank")
        return value


class ModelWorkEventRow(BaseModel):
    """One row in ``omninode_internal.work_events``.

    Column semantics are documented once, authoritatively, in the migration
    (``migrations/0001_create_work_events.sql`` section 4).
    """

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(..., min_length=1)
    emitted_at: datetime
    event_kind: str = Field(..., min_length=1)
    actor_kind: EnumActorKind = Field(default=EnumActorKind.SESSION)
    actor_id: str = Field(..., min_length=1)
    ticket_id: str | None = Field(default=None)
    summary: str = Field(default="", max_length=MAX_SUMMARY_CHARS)
    source_topic: str = Field(..., min_length=1)
    payload: dict[str, object] = Field(default_factory=dict)


class ModelProjectionWorkEventsResult(BaseModel):
    """Output of one projection operation."""

    model_config = ConfigDict(frozen=True)

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default="work_events")


def derive_event_id(
    *,
    source_topic: str,
    actor_id: str,
    emitted_at: datetime,
    payload: dict[str, object],
) -> str:
    """Content-address a work event, for idempotency on replay.

    The digest covers the full identity of the record -- topic, actor, event
    time and the canonicalized payload -- so replaying the same stream twice
    UPSERTs onto the same primary keys and leaves the table byte-identical
    (OMN-16180 acceptance 2), while two genuinely distinct events never collide.

    This deliberately does NOT use a per-session sequence counter. The sibling
    ``node_projection_session_replay`` derives its key from
    ``(session_id, sequence)`` but threads no reducer state across dispatches,
    so ``sequence`` is always 0 and its ``UNIQUE (session_id, sequence)``
    collapses an entire session onto ONE row -- live-observed on the stability
    lane 2026-08-29 (14 rows total against a topic carrying thousands of
    records). A content-addressed key cannot degrade that way, and needs no
    cross-dispatch state to be correct.

    ``sort_keys=True`` and the explicit separators make the canonical form
    stable across Python versions and dict insertion order.
    """
    canonical_payload = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(
        "\x00".join(
            (source_topic, actor_id, emitted_at.isoformat(), canonical_payload)
        ).encode("utf-8")
    ).hexdigest()
    return (
        f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    )


__all__: list[str] = [
    "MAX_SUMMARY_CHARS",
    "EnumActorKind",
    "EnumWorkEventKind",
    "ModelProjectionWorkEventsResult",
    "ModelWorkEventInbound",
    "ModelWorkEventRow",
    "WorkEventProjectionError",
    "derive_event_id",
]

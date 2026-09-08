# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Models for node_projection_session_replay.

[OMN-13087]
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _require_timezone_aware(value: object) -> object:
    """Parse an ISO-8601 timestamp and REFUSE a timezone-naive one.

    [OMN-17862] Deliberately strict, and deliberately NOT the wire's shared
    ``TimezoneAwareDatetime`` annotation. That annotation delegates to
    ``ensure_timezone_aware`` with its default ``assume_utc=True``, which STAMPS
    UTC onto a naive value and logs a warning instead of raising -- a defaulted
    value wearing a validator. A plain ``datetime`` field is no better: pydantic
    does not require ``tzinfo``.

    Both matter here because the parsed value is normalized with
    ``astimezone(UTC)`` before it is stored, and ``datetime.astimezone`` on a
    NAIVE value assumes the HOST's local zone and converts -- silently shifting
    the stored value by whatever offset the runtime container happens to run at,
    straight into this exposure's declared freshness column. With the naive case
    refused here, ``astimezone`` can only ever see an aware value, where it is a
    pure normalization.

    Raises ``ValueError``, which pydantic wraps into a ``ValidationError``. That
    type is what the runtime's keep-or-ack allowlist
    (``handler_wiring._is_projection_content_failure``, a CLOSED allowlist of
    ``PydanticValidationError | EnvelopeValidationError``) accepts, so the
    refusal reaches the DLQ-and-ack arm and the offset advances. A ``ValueError``
    SUBCLASS, a custom exception, or ``ProtocolConfigurationError`` raised from
    the handler body would all miss that allowlist, set ``write_path_failure``,
    raise ``ProjectionNotMaterializedError`` and withhold the offset -- the
    permanent partition stall this repair exists to END, re-created by its own
    refusal.

    ``datetime.fromisoformat`` plus a ``tzinfo``/``utcoffset`` check is also,
    BY CONSTRUCTION, the predicate the pre-ship backlog census used to prove the
    stalled backlog carries this field. Declared any more loosely, the parse
    would accept records the census counted as misses -- a green census over a
    backlog the fix then quarantines.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"emitted_at must be an ISO-8601 timestamp, got {value!r}"
            ) from exc
    else:
        raise ValueError(
            f"emitted_at must be an ISO-8601 timestamp string or datetime, "
            f"got {type(value).__name__}"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"emitted_at must be timezone-aware; {parsed.isoformat()} carries no "
            "offset and this projection refuses to invent one"
        )
    return parsed


TimezoneAwareEmittedAt = Annotated[datetime, BeforeValidator(_require_timezone_aware)]

# ---------------------------------------------------------------------------
# Inbound event models
# ---------------------------------------------------------------------------


class ModelSessionReplayEvent(BaseModel):
    """Inbound event from any subscribed session lifecycle topic.

    Topic-specific fields are optional so the same model can receive events from
    heterogeneous topics (session-started, prompt-submitted, tool-executed,
    session-outcome, session-ended). Unknown fields are ignored per
    ``extra="ignore"`` so replay is not broken by schema additions in
    upstream emitters.

    [OMN-17862] ``emitted_at`` is the exception: it is REQUIRED, because it is
    required on the wire for all five subscribed topics and because row identity
    now depends on it. Declaring it here is also what stopped ``extra="ignore"``
    from DISCARDING it -- the model used to declare ``timestamp``, which no
    subscribed producer emits, so it parsed to ``None`` and ``_build_row``
    substituted a wall-clock read on every single delivery. Two deliveries of one
    event then stored two different timestamps under one identity. ``timestamp``
    is gone from this model for that reason: an inbound field that no producer
    sends is not a source, it is the trapdoor the clock fell through.

    ``tool_execution_id`` / ``prompt_id`` are the per-topic source-event ids.
    They are declared and STRICTLY TYPED -- a present-but-malformed value refuses
    at parse, matching the wire's own ``UUID`` declaration -- but they are
    OPTIONAL, and that is a measured decision rather than a lenient one. The
    plan of record specified them as required. Its own pre-ship backlog control,
    run read-only against the stalled consumer groups' committed offsets on the
    .201 stability lane 2026-09-07T23:2xZ, measured **0 of 3000**
    ``tool-executed`` records carrying a ``tool_execution_id`` (with
    ``wrong_topic_id`` also 0 -- absent, not mistyped) against **3000 of 3000**
    carrying a timezone-aware ``emitted_at``; ``prompt-submitted`` carries no
    ``prompt_id`` either. Requiring the id would have refused 100% of a
    330,176-record backlog into a quarantine sink that nothing subscribes to and
    that already held 8,878,926 records -- the one-way door this repair exists to
    avoid, not the repair. So the plan's own rule for a topic with no per-event
    id applies to these two as well: the identity falls back to ``emitted_at``
    plus the topic and session id and that topic's own distinguishing fields.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str = Field(..., description="Unique session identifier.")
    emitted_at: TimezoneAwareEmittedAt = Field(
        ...,
        description=(
            "Source timestamp the producer emitted, timezone-aware. REQUIRED: it "
            "is the row's stored timestamp and part of its identity, and a "
            "delivery without one is refused rather than clock-stamped."
        ),
    )

    # Per-topic source-event identifiers (OMN-17862). Typed so a malformed value
    # refuses at parse; optional because the live wire does not carry them.
    tool_execution_id: UUID | None = Field(
        default=None,
        description="Per-execution id on tool-executed, when the producer emits one.",
    )
    prompt_id: UUID | None = Field(
        default=None,
        description="Per-prompt id on prompt-submitted, when the producer emits one.",
    )

    # Prompt-submitted fields
    prompt_preview: str | None = Field(
        default=None, description="Sanitized 100-char prompt preview."
    )
    prompt_length: int | None = Field(
        default=None, description="Full prompt length in characters.", ge=0
    )

    # Tool-executed fields
    tool_name: str | None = Field(
        default=None, description="Name of the executed tool."
    )
    tool_input: dict[str, object] | None = Field(
        default=None, description="Tool input arguments."
    )

    # Session-outcome fields
    outcome: str | None = Field(
        default=None,
        description="Session outcome: success, failed, abandoned, unknown.",
    )

    # Token budget fields (present on various events)
    tokens_used: int | None = Field(
        default=None, description="Tokens consumed by this event.", ge=0
    )
    total_tokens: int | None = Field(
        default=None, description="Cumulative tokens at time of event.", ge=0
    )


# ---------------------------------------------------------------------------
# Projection row model
# ---------------------------------------------------------------------------


class ModelReplaySnapshotRow(BaseModel, frozen=True):
    """One row in the session_replay_snapshots table.

    Schema mirrors the ReplaySnapshot TypeScript interface consumed by the
    omnidash SessionReplayPage widget.
    """

    snapshot_id: str = Field(..., description="Unique snapshot identifier (UUID).")
    session_id: str = Field(..., description="Session the snapshot belongs to.")
    sequence: int = Field(
        ..., ge=0, description="Zero-based ordering within the session."
    )
    timestamp: str = Field(..., description="ISO 8601 timestamp of the event.")
    event_type: str = Field(
        ...,
        description=(
            "Event classification: session_start | user_input | tool_call | "
            "checkpoint | session_end"
        ),
    )
    node_name: str = Field(
        ..., description="Name of the node or actor that produced the event."
    )
    state_delta: dict[str, object] = Field(
        default_factory=dict,
        description="State changes introduced by this event.",
    )
    cumulative_tokens: int = Field(
        default=0,
        ge=0,
        description="Running total of tokens consumed up to this event.",
    )
    is_checkpoint: bool = Field(
        default=False,
        description="True for session_start, session_end, and outcome events.",
    )


# ---------------------------------------------------------------------------
# Reducer state model
# ---------------------------------------------------------------------------


class ModelSessionReplayState(BaseModel):
    """In-memory reduction state accumulated per session.

    Tracks the current sequence counter and cumulative token count so that each
    call to ``accumulate()`` can produce a self-consistent snapshot row without
    requiring a DB read.
    """

    model_config = ConfigDict(frozen=False)

    sequence: int = Field(
        default=0, ge=0, description="Next sequence number to assign."
    )
    cumulative_tokens: int = Field(
        default=0, ge=0, description="Running token total for this session."
    )


# ---------------------------------------------------------------------------
# Handler output model
# ---------------------------------------------------------------------------


class ModelProjectionReplayResult(BaseModel):
    """Output from one projection operation."""

    model_config = ConfigDict(frozen=True)

    rows_upserted: int = Field(default=0, ge=0)
    snapshot_published: bool = Field(
        default=False,
        description=(
            "Whether the row's snapshot delta reached the bus (OMN-17774). "
            "False on a durable write whose republish leg was unavailable, and "
            "False on an exposure that is not bus_backed at all. The runtime "
            "gates its terminal event on rows_upserted, not on this, because "
            "the row IS durable either way -- but a republish that silently "
            "did not happen is the 'consume, ack, render nothing, log nothing' "
            "shape epic OMN-16776 exists to close, so it is reported rather "
            "than swallowed."
        ),
    )
    table: str = Field(default="session_replay_snapshots")


__all__: list[str] = [
    "ModelProjectionReplayResult",
    "ModelReplaySnapshotRow",
    "ModelSessionReplayEvent",
    "ModelSessionReplayState",
    "TimezoneAwareEmittedAt",
]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Models for node_projection_session_replay.

[OMN-13087]
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Inbound event models
# ---------------------------------------------------------------------------


class ModelSessionReplayEvent(BaseModel):
    """Inbound event from any subscribed session lifecycle topic.

    Fields are declared as optional so the same model can receive events from
    heterogeneous topics (session-started, prompt-submitted, tool-executed,
    session-outcome, session-ended). Unknown fields are ignored per
    ``extra="ignore"`` so replay is not broken by schema additions in
    upstream emitters.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    session_id: str = Field(..., description="Unique session identifier.")
    timestamp: str | None = Field(default=None, description="ISO 8601 event timestamp.")

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
    table: str = Field(default="session_replay_snapshots")


__all__: list[str] = [
    "ModelProjectionReplayResult",
    "ModelReplaySnapshotRow",
    "ModelSessionReplayEvent",
    "ModelSessionReplayState",
]

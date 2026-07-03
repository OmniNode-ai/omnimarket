# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerProjectionSessionReplay — reduce session events into replay snapshots.

Consumes session lifecycle events from the five contract-declared subscribe
topics and maps them to replay event types.

Each event produces one row in session_replay_snapshots. A per-session
sequence counter and cumulative token total are carried in
ModelSessionReplayState. UPSERT key is snapshot_id (UUID derived from
session_id + sequence) so the projection is idempotent on replay.

[OMN-13087]
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.contract_topics import contract_subscribe_topics
from omnimarket.nodes.node_projection_session_replay.models.model_session_replay import (
    ModelProjectionReplayResult,
    ModelReplaySnapshotRow,
    ModelSessionReplayEvent,
    ModelSessionReplayState,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

logger = logging.getLogger(__name__)

TABLE = "session_replay_snapshots"
CONFLICT_KEY = "snapshot_id"
_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


@dataclass(frozen=True)
class _SessionReplayTopics:
    session_started: str
    prompt_submitted: str
    tool_executed: str
    session_outcome: str
    session_ended: str


def _load_topics(contract_path: Path | None = None) -> _SessionReplayTopics:
    topics = contract_subscribe_topics(contract_path or _DEFAULT_CONTRACT_PATH)
    if len(topics) != 5:
        raise ValueError(
            "node_projection_session_replay contract must declare exactly "
            f"five subscribe topics; found {len(topics)}."
        )
    return _SessionReplayTopics(
        session_started=topics[0],
        prompt_submitted=topics[1],
        tool_executed=topics[2],
        session_outcome=topics[3],
        session_ended=topics[4],
    )


_TOPICS = _load_topics()
TOPIC_SESSION_STARTED = _TOPICS.session_started
TOPIC_PROMPT_SUBMITTED = _TOPICS.prompt_submitted
TOPIC_TOOL_EXECUTED = _TOPICS.tool_executed
TOPIC_SESSION_OUTCOME = _TOPICS.session_outcome
TOPIC_SESSION_ENDED = _TOPICS.session_ended

# Topic → (event_type, node_name, is_checkpoint)
_TOPIC_MAP: dict[str, tuple[str, str, bool]] = {
    TOPIC_SESSION_STARTED: ("session_start", "session", True),
    TOPIC_PROMPT_SUBMITTED: ("user_input", "user", False),
    TOPIC_TOOL_EXECUTED: ("tool_call", "", False),
    TOPIC_SESSION_OUTCOME: ("checkpoint", "session", True),
    TOPIC_SESSION_ENDED: ("session_end", "session", True),
}


def _derive_snapshot_id(session_id: str, sequence: int) -> str:
    """Deterministic UUID-shaped identifier from session_id and sequence.

    Uses SHA-256 so replay is fully idempotent regardless of DB ordering.
    """
    digest = hashlib.sha256(f"{session_id}::{sequence}".encode()).hexdigest()
    # Format as UUID to match dashboard expectations.
    return (
        f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    )


def _classify_event(
    topic: str,
    event: ModelSessionReplayEvent,
) -> tuple[str, str, bool]:
    """Return (event_type, node_name, is_checkpoint) for the given topic."""
    if topic in _TOPIC_MAP:
        event_type, node_name, is_checkpoint = _TOPIC_MAP[topic]
        # For tool_call events, node_name is the tool's own name when available.
        if event_type == "tool_call" and event.tool_name:
            node_name = event.tool_name
        return event_type, node_name, is_checkpoint
    # Unknown topic: emit a generic event rather than silently drop.
    return "event", "unknown", False


def _extract_state_delta(
    topic: str,
    event: ModelSessionReplayEvent,
) -> dict[str, object]:
    """Extract a minimal state delta for the event type."""
    if topic == TOPIC_SESSION_STARTED:
        return {"session_id": event.session_id}
    if topic == TOPIC_PROMPT_SUBMITTED:
        delta: dict[str, object] = {}
        if event.prompt_preview is not None:
            delta["prompt_preview"] = event.prompt_preview
        if event.prompt_length is not None:
            delta["prompt_length"] = event.prompt_length
        return delta
    if topic == TOPIC_TOOL_EXECUTED:
        delta2: dict[str, object] = {}
        if event.tool_name is not None:
            delta2["tool_name"] = event.tool_name
        if event.tool_input is not None:
            delta2["tool_input"] = event.tool_input
        return delta2
    if topic == TOPIC_SESSION_OUTCOME:
        return {"outcome": event.outcome or "unknown"}
    if topic == TOPIC_SESSION_ENDED:
        return {"session_id": event.session_id}
    return {}


class HandlerProjectionSessionReplay:
    """Pure reducer: accumulate session events into replay snapshot rows."""

    def accumulate(
        self,
        state: ModelSessionReplayState,
        event: ModelSessionReplayEvent,
        topic: str,
    ) -> tuple[ModelSessionReplayState, ModelReplaySnapshotRow]:
        """Reduce one event into a snapshot row and advance state.

        Returns:
            Updated state and the new snapshot row to persist.
        """
        now = datetime.now(tz=UTC).isoformat()
        timestamp = event.timestamp or now

        event_type, node_name, is_checkpoint = _classify_event(topic, event)
        state_delta = _extract_state_delta(topic, event)

        token_increment = event.tokens_used or 0
        new_cumulative = state.cumulative_tokens + token_increment
        sequence = state.sequence

        snapshot_id = _derive_snapshot_id(event.session_id, sequence)

        row = ModelReplaySnapshotRow(
            snapshot_id=snapshot_id,
            session_id=event.session_id,
            sequence=sequence,
            timestamp=timestamp,
            event_type=event_type,
            node_name=node_name,
            state_delta=state_delta,
            cumulative_tokens=new_cumulative,
            is_checkpoint=is_checkpoint,
        )

        new_state = ModelSessionReplayState(
            sequence=sequence + 1,
            cumulative_tokens=new_cumulative,
        )

        return new_state, row

    def project(
        self,
        event: ModelSessionReplayEvent,
        db: DatabaseAdapter,
        topic: str,
        state: ModelSessionReplayState | None = None,
    ) -> ModelProjectionReplayResult:
        """Project one event to the session_replay_snapshots table.

        Args:
            event: Inbound session event.
            db: Sync database adapter for UPSERT.
            topic: Source topic string (determines event classification).
            state: Per-session reducer state; if None a fresh state is used.
                   Callers managing multi-event sessions should thread the
                   returned state through subsequent calls.

        Returns:
            Projection result with rows_upserted count.
        """
        current_state = state or ModelSessionReplayState()
        _, row = self.accumulate(current_state, event, topic)
        row_dict: dict[str, object] = {
            "snapshot_id": row.snapshot_id,
            "session_id": row.session_id,
            "sequence": row.sequence,
            "timestamp": row.timestamp,
            "event_type": row.event_type,
            "node_name": row.node_name,
            "state_delta": row.state_delta,
            "cumulative_tokens": row.cumulative_tokens,
            "is_checkpoint": row.is_checkpoint,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row_dict)
        return ModelProjectionReplayResult(rows_upserted=1 if ok else 0)

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Expects ``_db`` (DatabaseAdapter) and ``_topic`` in input_data.
        """
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        topic = str(input_data.pop("_topic", TOPIC_SESSION_STARTED))
        event = ModelSessionReplayEvent(**input_data)
        result = self.project(event, db_raw, topic)
        return result.model_dump(mode="json")


__all__: list[str] = ["HandlerProjectionSessionReplay"]

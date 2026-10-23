# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold for the full-content session projection (OMN-19550).

One content record in, one row out. No clock, no broker, no database, so every
claim about a row can be falsified by a unit test. The writer beside it calls
exactly this function, so the durable row and the tested row cannot disagree.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from omnimarket.nodes.contract_topics import contract_subscribe_topics
from omnimarket.nodes.node_projection_session_content.models.model_session_content import (
    ModelSessionContentRecord,
    ModelSessionContentRow,
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


class SessionContentFoldError(ValueError):
    """A record this projection refuses rather than stores under a guess."""


#: OMN-21123 (TA.21): the content kind of a barrier record. A barrier rides the
#: content topic beside the chunks and carries ids, a count and a flag.
BARRIER_CONTENT_KIND = "barrier"

#: Identity fields the capture producer stamps (TA.21). Each joins the key only
#: when the record carries it, so a record from before the stamp derives the
#: key it always had and a replay of it lands on its old row.
_STAMPED_IDENTITY = (
    "content_record_id",
    "stream",
    "producer_seq",
    "barrier_id",
    "high_water",
)


def _load_topic() -> str:
    topics = contract_subscribe_topics(_CONTRACT_PATH)
    if len(topics) != 1:
        raise SessionContentFoldError(
            "node_projection_session_content must subscribe exactly one topic; "
            f"its contract declares {len(topics)}"
        )
    return topics[0]


#: The one topic this node folds, resolved from the contract, never a literal.
SUBSCRIBE_TOPIC = _load_topic()

#: Postgres TEXT and JSONB cannot hold U+0000. A tool result can (a binary
#: file read, a NUL-separated `find -print0`). The character is replaced with
#: U+FFFD rather than dropped, so the row keeps its length and says a
#: character was there.
_NUL = "\x00"
_REPLACEMENT = "�"


def _pg_safe(value: object) -> object:
    if isinstance(value, str):
        return value.replace(_NUL, _REPLACEMENT)
    if isinstance(value, dict):
        return {str(k): _pg_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_pg_safe(v) for v in value]
    return value


def derive_event_id(record: ModelSessionContentRecord, *, source_topic: str) -> str:
    """Content-address one record, so a replay of it lands on the same row."""
    identity = {
        "source_topic": source_topic,
        "session_id": record.session_id,
        "turn_id": record.turn_id,
        "tool_use_id": record.tool_use_id,
        "content_kind": record.content_kind,
        "chunk_index": record.chunk_index,
        "content_sha256": record.content_sha256,
        "emitted_at": record.emitted_at.isoformat(),
    }
    # One barrier call journals a barrier per stream under one emitted_at, and
    # two stamped records of one hook call can share every field above; without
    # the stamp they would collapse onto one row.
    for name in _STAMPED_IDENTITY:
        value = getattr(record, name)
        if value is not None:
            identity[name] = value
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def fold_session_content(
    record: ModelSessionContentRecord, *, source_topic: str = SUBSCRIBE_TOPIC
) -> ModelSessionContentRow:
    """Reduce one content record to its row.

    Raises:
        SessionContentFoldError: the record carries no session id. Without it
            the row joins to nothing, and a row that joins to nothing is not
            training data, it is noise.
    """
    if not record.session_id.strip():
        raise SessionContentFoldError("a content record needs a non-empty session_id")
    if record.content_kind == BARRIER_CONTENT_KIND and (
        record.stream is None or not record.barrier_id or record.high_water is None
    ):
        raise SessionContentFoldError(
            "a barrier record needs its stream, barrier_id and high_water"
        )
    command = _pg_safe(record.command)
    return ModelSessionContentRow(
        event_id=derive_event_id(record, source_topic=source_topic),
        session_id=record.session_id,
        turn_id=record.turn_id,
        correlation_id=record.correlation_id,
        tool_use_id=record.tool_use_id,
        tool_name=record.tool_name,
        content_kind=record.content_kind,
        chunk_index=record.chunk_index,
        chunk_count=record.chunk_count,
        content=str(_pg_safe(record.content)),
        command=command,
        content_sha256=record.content_sha256,
        original_chars=record.original_chars,
        truncated=record.truncated,
        redaction_state=record.redaction_state,
        producer_redaction=dict(record.producer_redaction),
        hook_source=record.hook_source,
        emitted_at=record.emitted_at,
        source_topic=source_topic,
        content_record_id=record.content_record_id,
        stream=record.stream,
        producer_seq=record.producer_seq,
        record_type=record.record_type,
        barrier_id=record.barrier_id,
        high_water=record.high_water,
        content_capture=record.content_capture,
    )


__all__ = [
    "BARRIER_CONTENT_KIND",
    "SUBSCRIBE_TOPIC",
    "SessionContentFoldError",
    "derive_event_id",
    "fold_session_content",
]

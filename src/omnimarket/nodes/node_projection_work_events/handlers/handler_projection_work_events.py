# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerProjectionWorkEvents -- project live session events into work_events.

[OMN-16180] The L1 rung of the OMN-16176 ledger ladder. Pure reducer: one
inbound event maps to exactly one row, with no cross-dispatch state. UPSERT key
is the content-addressed ``event_id``, so replay is idempotent.

Deliberately NOT in scope here (each has its own open ticket, none is silently
assumed):

* the five C1 ``work.claim.*`` / ``work.result.*`` kinds -- OMN-16177 schema is
  merged (omnibase_core#1563) but the C2 emit path that publishes them is not
  built, so this node projects the kinds that actually flow today;
* claims arbitration and the ``work_claims`` table -- OMN-16179 (C3), whose
  Rev 2.1 correction puts arbitration on a compacted Kafka view rather than in
  Postgres anyway;
* retiring ``ledger_lock.py`` -- OMN-16183 (C7). L0
  (``docs/tracking/ROLLING_WORK_LEDGER.md``) stays authoritative and dual-write
  is unchanged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from omnimarket.nodes.contract_topics import contract_subscribe_topics
from omnimarket.nodes.node_projection_work_events.models.model_work_event import (
    MAX_SUMMARY_CHARS,
    EnumActorKind,
    EnumWorkEventKind,
    ModelProjectionWorkEventsResult,
    ModelWorkEventInbound,
    ModelWorkEventRow,
    WorkEventProjectionError,
    derive_event_id,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

logger = logging.getLogger(__name__)

# BARE table name, deliberately. ``PostgresSyncProjectionAdapter._validate_
# identifier`` matches ``^[a-zA-Z_][a-zA-Z0-9_]*$`` and REJECTS a dotted name,
# so passing "omninode_internal.work_events" to ``db.upsert`` raises
# ``invalid table identifier`` on the real adapter while passing cleanly against
# the in-memory double. Schema qualification is the runtime's job, resolved from
# this contract's ``db_io.db_tables[0].schema`` -- the same split
# ``node_projection_live_events`` uses for its own omninode_internal table.
# Caught by the OMN-16180 real-Postgres write-path test, not by unit tests.
TABLE = "work_events"
SCHEMA = "omninode_internal"
CONFLICT_KEY = "event_id"
_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


@dataclass(frozen=True)
class _WorkEventTopics:
    """The four contract-declared subscribe topics, resolved by position."""

    session_started: str
    prompt_submitted: str
    tool_executed: str
    session_ended: str


def _load_topics(contract_path: Path | None = None) -> _WorkEventTopics:
    """Resolve subscribe topics from the contract, never from a literal.

    Hardcoded topic strings are an aislop-sweep finding and drift silently from
    the contract that actually drives the subscription.
    """
    topics = contract_subscribe_topics(contract_path or _DEFAULT_CONTRACT_PATH)
    if len(topics) != 4:
        raise ValueError(
            "node_projection_work_events contract must declare exactly four "
            f"subscribe topics; found {len(topics)}."
        )
    return _WorkEventTopics(
        session_started=topics[0],
        prompt_submitted=topics[1],
        tool_executed=topics[2],
        session_ended=topics[3],
    )


_TOPICS = _load_topics()
TOPIC_SESSION_STARTED = _TOPICS.session_started
TOPIC_PROMPT_SUBMITTED = _TOPICS.prompt_submitted
TOPIC_TOOL_EXECUTED = _TOPICS.tool_executed
TOPIC_SESSION_ENDED = _TOPICS.session_ended

_TOPIC_KIND: dict[str, EnumWorkEventKind] = {
    TOPIC_SESSION_STARTED: EnumWorkEventKind.SESSION_STARTED,
    TOPIC_PROMPT_SUBMITTED: EnumWorkEventKind.SESSION_PROMPT,
    TOPIC_TOOL_EXECUTED: EnumWorkEventKind.SESSION_TOOL,
    TOPIC_SESSION_ENDED: EnumWorkEventKind.SESSION_ENDED,
}


def _summarize(kind: EnumWorkEventKind, event: ModelWorkEventInbound) -> str:
    """Render the bounded narrative line for one event.

    Truncation is explicit and marked, never silent: a summary that lost its
    tail says so, because a ledger line that reads complete while being cut is
    worse than one that admits it.
    """
    if kind is EnumWorkEventKind.SESSION_STARTED:
        where = event.working_directory or "unknown directory"
        summary = f"session started in {where}"
    elif kind is EnumWorkEventKind.SESSION_PROMPT:
        length = event.prompt_length
        summary = (
            "prompt submitted"
            if length is None
            else f"prompt submitted ({length} chars)"
        )
    elif kind is EnumWorkEventKind.SESSION_TOOL:
        tool = event.tool_name or "unknown tool"
        summary = f"tool {tool}"
        if event.duration_ms is not None:
            summary = f"{summary} ({event.duration_ms} ms)"
        if event.interrupted:
            summary = f"{summary} [interrupted]"
    else:
        reason = event.reason or "unspecified"
        summary = f"session ended ({reason})"

    if len(summary) > MAX_SUMMARY_CHARS:
        marker = "... [truncated]"
        return summary[: MAX_SUMMARY_CHARS - len(marker)] + marker
    return summary


def _projected_payload(event: ModelWorkEventInbound) -> dict[str, object]:
    """Fields kept on the row but not promoted to their own column.

    Only keys with a value are written, so the payload of a session-ended event
    does not carry a wall of nulls belonging to tool events.
    """
    candidates: dict[str, object | None] = {
        "working_directory": event.working_directory,
        "hook_source": event.hook_source,
        "correlation_id": event.correlation_id,
        "prompt_length": event.prompt_length,
        "tool_name": event.tool_name,
        "duration_ms": event.duration_ms,
        "interrupted": event.interrupted,
        "reason": event.reason,
    }
    return {key: value for key, value in candidates.items() if value is not None}


class HandlerProjectionWorkEvents:
    """Pure reducer: one work event in, one work_events row out."""

    def accumulate(self, event: ModelWorkEventInbound, topic: str) -> ModelWorkEventRow:
        """Reduce one inbound event to its row. Pure -- no I/O, no state.

        Raises:
            WorkEventProjectionError: the topic is not one this node declares.
                Refused loudly rather than projected under a guessed kind, so an
                unknown or mis-routed event cannot enter the ledger wearing a
                label nothing produced (doctrine section 9).
        """
        kind = _TOPIC_KIND.get(topic)
        if kind is None:
            raise WorkEventProjectionError(
                f"topic {topic!r} is not declared by node_projection_work_events; "
                f"declared topics are {sorted(_TOPIC_KIND)}"
            )

        payload = _projected_payload(event)
        return ModelWorkEventRow(
            event_id=derive_event_id(
                source_topic=topic,
                actor_id=event.session_id,
                emitted_at=event.emitted_at,
                payload=payload,
            ),
            emitted_at=event.emitted_at,
            event_kind=kind.value,
            # The omniclaude hooks are session actors. A node actor reaches this
            # surface unchanged when C8 (OMN-16190) converges node workflows.
            actor_kind=EnumActorKind.SESSION,
            actor_id=event.session_id,
            # Hook events carry no ticket reference; the C2 emit path populates
            # this when work events start carrying one.
            ticket_id=None,
            summary=_summarize(kind, event),
            source_topic=topic,
            payload=payload,
        )

    def project(
        self,
        event: ModelWorkEventInbound,
        db: DatabaseAdapter,
        topic: str,
    ) -> ModelProjectionWorkEventsResult:
        """Project one event into ``omninode_internal.work_events``."""
        row = self.accumulate(event, topic)
        row_dict: dict[str, object] = {
            "event_id": row.event_id,
            # A ``datetime``, never ``.isoformat()``. asyncpg refuses a str for a
            # TIMESTAMPTZ parameter outright ("expected a datetime.date or
            # datetime.datetime instance, got 'str'"), and psycopg would only
            # accept one via an implicit text->timestamptz cast. Both adapters
            # take a real datetime, so the typed value is the portable one.
            "emitted_at": row.emitted_at,
            "event_kind": row.event_kind,
            "actor_kind": row.actor_kind.value,
            "actor_id": row.actor_id,
            "ticket_id": row.ticket_id,
            "summary": row.summary,
            "source_topic": row.source_topic,
            "payload": row.payload,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row_dict)
        return ModelProjectionWorkEventsResult(rows_upserted=1 if ok else 0)

    def handle(self, request: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal def-B handler entrypoint. Delegates to :meth:`project`.

        ``request`` is a magic single-positional-param name the shared
        ``runtime_local_adapter`` recognizes and adapts (OMN-14355 canonical
        handler shape) -- unlike the name ``input_data``, which the canon-shape
        ratchet classifies as a nonadaptable, non-canonical signature and
        hard-fails for a NEW node. The same rename is the documented remedy on
        ``node_projection_tenant_credentials``; the runtime still hands this
        method the injected payload dict described below, so the rename changes
        the classification, not the wire contract.

        Expects ``_db`` (DatabaseAdapter) and ``_topic`` in ``request``.
        ``_topic`` is REQUIRED and has no default: defaulting it would let a
        mis-routed message be projected under the wrong kind, which is the
        silent-corruption mode this projection exists to avoid.
        """
        db_raw = request.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in request['_db']")
        topic_raw = request.pop("_topic", None)
        if not isinstance(topic_raw, str) or not topic_raw:
            raise WorkEventProjectionError(
                "handle() requires the source topic in request['_topic']"
            )
        event = ModelWorkEventInbound(**request)
        result = self.project(event, db_raw, topic_raw)
        return result.model_dump(mode="json")


__all__: list[str] = ["HandlerProjectionWorkEvents"]

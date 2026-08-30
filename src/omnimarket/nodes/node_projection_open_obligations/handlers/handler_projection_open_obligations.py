# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerProjectionOpenObligations -- fold obligation events into what is owed.

[OMN-17019] C9 of the OMN-16176 ledger ladder. Pure reducer: one obligation
event maps to a targeted-column UPSERT on exactly one row, keyed on
``obligation_id``, with NO cross-dispatch state.

THE ONE INVARIANT THIS HANDLER EXISTS TO HOLD
    Each event kind writes ONLY the columns its kind owns
    (``_COLUMNS_BY_KIND``). It never writes ``state`` or ``owed_by`` -- those
    are Postgres GENERATED columns over the owned columns -- and only the three
    terminal kinds write ``closed_state``.

    That is what makes the fold correct under the case that actually happens in
    production: a consumer restarting from an EARLIER partition offset
    re-delivers ``created`` after ``satisfied`` was already applied. Since
    ``created`` never touches ``closed_state``, a re-delivery cannot reopen a
    closed obligation. A handler that wrote a ``state`` column would silently
    reopen it and the projection would report as owed something delivered days
    ago -- undetectably, because the row would look perfectly well-formed.

Deliberately NOT in scope here, each with its own open ticket, none silently
assumed:

* claims arbitration and leases -- OMN-16179 (C3) keeps arbitration on a
  compacted Kafka view, deliberately not in Postgres, so a reducer outage
  degrades rendering only and never the ability to claim work. Nothing in this
  node is on the claim path;
* any expiry, TTL or sweep. An obligation leaves the open set exactly one way:
  a recorded terminal event naming its own evidence. There is no code path here
  that removes a row, and the migration grants the runtime role no DELETE;
* retiring the markdown surfaces -- OMN-16183 (C7). They keep working; this
  ticket makes them renderable FROM the projection rather than authoritative.
"""

from __future__ import annotations

import logging
from pathlib import Path

from omnimarket.nodes.contract_topics import contract_subscribe_topics
from omnimarket.nodes.node_projection_open_obligations.models.model_obligation_event import (
    CLOSED_STATE_BY_KIND,
    REQUIRED_FIELDS_BY_KIND,
    TERMINAL_KINDS,
    EnumObligationEventKind,
    ModelObligationEventInbound,
    ModelOpenObligationRow,
    ModelProjectionOpenObligationsResult,
    ObligationProjectionError,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

logger = logging.getLogger(__name__)

# BARE table name, deliberately. ``PostgresSyncProjectionAdapter._validate_
# identifier`` matches ``^[a-zA-Z_][a-zA-Z0-9_]*$`` and REJECTS a dotted name,
# so passing "omninode_internal.open_obligations" to ``db.upsert`` raises
# ``invalid table identifier`` on the real adapter while passing cleanly against
# the in-memory double. Schema qualification is the runtime's job, resolved from
# this contract's ``db_io.db_tables[0].schema`` -- the same split the sibling
# node_projection_work_events uses for its own omninode_internal table.
TABLE = "open_obligations"
SCHEMA = "omninode_internal"
CONFLICT_KEY = "obligation_id"

_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"

# Declaration order of EnumObligationEventKind IS the contract's declared topic
# order. Both are asserted against each other at import time by _load_topics.
_KIND_ORDER: tuple[EnumObligationEventKind, ...] = (
    EnumObligationEventKind.CREATED,
    EnumObligationEventKind.TRANSFERRED,
    EnumObligationEventKind.SATISFIED,
    EnumObligationEventKind.SUPERSEDED,
    EnumObligationEventKind.ABANDONED,
)


def _load_topics(
    contract_path: Path | None = None,
) -> dict[str, EnumObligationEventKind]:
    """Resolve the topic -> kind map from the contract, never from a literal.

    Hardcoded topic strings are an aislop-sweep finding and drift silently from
    the contract that actually drives the subscription. Refusing to import on a
    count mismatch is what turns a reordered or truncated ``subscribe_topics``
    list into a loud failure rather than a projection that folds ``satisfied``
    events as ``created`` ones.
    """
    topics = contract_subscribe_topics(contract_path or _DEFAULT_CONTRACT_PATH)
    if len(topics) != len(_KIND_ORDER):
        raise ValueError(
            "node_projection_open_obligations contract must declare exactly "
            f"{len(_KIND_ORDER)} subscribe topics, one per obligation kind; "
            f"found {len(topics)}."
        )
    return dict(zip(topics, _KIND_ORDER, strict=True))


_TOPIC_KIND: dict[str, EnumObligationEventKind] = _load_topics()

# Columns each kind is permitted to write. Disjoint across kinds except for the
# shared header columns in _COMMON_COLUMNS and closed_state/closed_at, which
# only the three terminal kinds write. Read the migration's section 4 for why
# this partition is the schema's core invariant rather than a style choice.
_COMMON_COLUMNS: tuple[str, ...] = (
    "obligation_id",
    "last_event_kind",
    "last_event_at",
    "actor_kind",
    "actor_id",
    "source_topic",
    "payload",
)
_COLUMNS_BY_KIND: dict[EnumObligationEventKind, tuple[str, ...]] = {
    EnumObligationEventKind.CREATED: (
        "created_at",
        "asked_by",
        "original_owed_by",
        "acceptance_condition",
        "opened_summary",
        "ticket_id",
    ),
    EnumObligationEventKind.TRANSFERRED: ("transferred_owed_by",),
    EnumObligationEventKind.SATISFIED: (
        "closed_state",
        "closed_at",
        "evidence_uri",
        "delivery_state",
    ),
    EnumObligationEventKind.SUPERSEDED: (
        "closed_state",
        "closed_at",
        "superseded_by",
    ),
    EnumObligationEventKind.ABANDONED: (
        "closed_state",
        "closed_at",
        "abandon_reason",
    ),
}

# Fields promoted to their own column by SOME kind. Anything else with a value
# lands in ``payload`` so it is preserved but not silently treated as a fact the
# schema promises.
_PROMOTED_FIELDS: frozenset[str] = frozenset(
    {
        "obligation_id",
        "emitted_at",
        "actor_id",
        "actor_kind",
        "summary",
        "asked_by",
        "owed_by",
        "acceptance_condition",
        "ticket_id",
        "evidence_uri",
        "delivery_state",
        "superseded_by_obligation_id",
        "abandon_reason",
    }
)


def _kind_for_topic(topic: str) -> EnumObligationEventKind:
    """Resolve a topic to its kind, or refuse the event.

    Raises:
        ObligationProjectionError: the topic is not one this node declares.
            Refused rather than folded under a guessed kind, so a mis-routed
            event cannot enter the projection wearing a label nothing produced.
            A bare ``_TOPIC_KIND[topic]`` here would surface as a ``KeyError``,
            which the dispatch seam classifies as an internal fault rather than
            a rejected message -- the refusal has to be this node's own typed
            error to be routed and observed as one.
    """
    kind = _TOPIC_KIND.get(topic)
    if kind is None:
        raise ObligationProjectionError(
            f"topic {topic!r} is not declared by node_projection_open_obligations; "
            f"declared topics are {sorted(_TOPIC_KIND)}"
        )
    return kind


def _require_fields(
    kind: EnumObligationEventKind, event: ModelObligationEventInbound
) -> None:
    """Enforce the per-kind mandatory fields, or refuse the event.

    Raises:
        ObligationProjectionError: a field this kind cannot mean anything
            without is absent. Refused loudly rather than projected with a NULL,
            because obligation WRITES fail closed (off-rails rev 2): an
            obligation that cannot be recorded must block the close, never be
            dropped. A ``satisfied`` with no ``evidence_uri`` is precisely the
            "declared done, never delivered" record this surface exists to make
            impossible.
    """
    missing = [
        field
        for field in REQUIRED_FIELDS_BY_KIND[kind]
        if getattr(event, field) is None
    ]
    if missing:
        raise ObligationProjectionError(
            f"{kind.value} requires {sorted(REQUIRED_FIELDS_BY_KIND[kind])}; "
            f"missing {sorted(missing)} for obligation {event.obligation_id!r}"
        )


def _residual_payload(event: ModelObligationEventInbound) -> dict[str, object]:
    """Wire fields kept on the row but not promoted to their own column.

    Only keys with a value are written, so an ``abandoned`` event's payload does
    not carry a wall of nulls belonging to ``created``.
    """
    dumped = event.model_dump(mode="json")
    return {
        key: value
        for key, value in dumped.items()
        if key not in _PROMOTED_FIELDS and value is not None
    }


class HandlerProjectionOpenObligations:
    """Pure reducer: one obligation event in, one targeted-column row out."""

    def accumulate(
        self, event: ModelObligationEventInbound, topic: str
    ) -> ModelOpenObligationRow:
        """Reduce one inbound event to its row. Pure -- no I/O, no state.

        The returned model carries the FULL row shape; :meth:`project` narrows
        it to the columns this kind owns before writing. Keeping the narrowing
        in one place means a new kind cannot accidentally widen its write set by
        populating a field on the model.

        Raises:
            ObligationProjectionError: the topic is not one this node declares,
                or a per-kind mandatory field is absent. Refused rather than
                folded under a guessed kind, so a mis-routed event cannot enter
                the projection wearing a label nothing produced.
        """
        kind = _kind_for_topic(topic)
        _require_fields(kind, event)

        is_terminal = kind in TERMINAL_KINDS
        return ModelOpenObligationRow(
            obligation_id=event.obligation_id,
            last_event_kind=kind.value,
            last_event_at=event.emitted_at,
            actor_kind=event.actor_kind,
            actor_id=event.actor_id,
            source_topic=topic,
            payload=_residual_payload(event),
            # created-owned
            created_at=(
                event.emitted_at if kind is EnumObligationEventKind.CREATED else None
            ),
            asked_by=event.asked_by,
            original_owed_by=(
                event.owed_by if kind is EnumObligationEventKind.CREATED else None
            ),
            acceptance_condition=event.acceptance_condition,
            opened_summary=(
                event.summary if kind is EnumObligationEventKind.CREATED else None
            ),
            ticket_id=event.ticket_id,
            # transferred-owned
            transferred_owed_by=(
                event.owed_by if kind is EnumObligationEventKind.TRANSFERRED else None
            ),
            # terminal-owned
            closed_state=CLOSED_STATE_BY_KIND.get(kind),
            closed_at=event.emitted_at if is_terminal else None,
            evidence_uri=event.evidence_uri,
            delivery_state=event.delivery_state,
            superseded_by=event.superseded_by_obligation_id,
            abandon_reason=event.abandon_reason,
        )

    def project(
        self,
        event: ModelObligationEventInbound,
        db: DatabaseAdapter,
        topic: str,
    ) -> ModelProjectionOpenObligationsResult:
        """Project one obligation event into ``omninode_internal.open_obligations``.

        Writes ONLY the columns this event's kind owns. Every adapter's UPSERT
        is a targeted-column merge naming just the incoming columns (OMN-15598
        proved the three adapters agree on this byte-for-byte), so the columns
        omitted here survive untouched on the existing row -- which is how one
        row accumulates a whole lifecycle with no read-modify-write and no
        reducer state.
        """
        kind = _kind_for_topic(topic)
        row = self.accumulate(event, topic)
        dumped = row.model_dump(mode="python")

        row_dict: dict[str, object] = {}
        for column in (*_COMMON_COLUMNS, *_COLUMNS_BY_KIND[kind]):
            value = dumped[column]
            # StrEnum members round-trip as themselves under mode="python"; the
            # adapters want the plain wire string.
            row_dict[column] = value.value if hasattr(value, "value") else value

        ok = db.upsert(TABLE, CONFLICT_KEY, row_dict)
        return ModelProjectionOpenObligationsResult(rows_upserted=1 if ok else 0)

    def handle(self, request: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal def-B handler entrypoint. Delegates to :meth:`project`.

        ``request`` is a magic single-positional-param name the shared
        ``runtime_local_adapter`` recognizes and adapts (OMN-14355 canonical
        handler shape) -- unlike the name ``input_data``, which the canon-shape
        ratchet classifies as a nonadaptable, non-canonical signature and
        hard-fails for a NEW node.

        Expects ``_db`` (DatabaseAdapter) and ``_topic`` in ``request``.
        ``_topic`` is REQUIRED and has no default: defaulting it would let a
        mis-routed message be folded under the wrong kind, and folding a
        ``created`` as a ``satisfied`` would close an obligation nobody
        delivered.
        """
        db_raw = request.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in request['_db']")
        topic_raw = request.pop("_topic", None)
        if not isinstance(topic_raw, str) or not topic_raw:
            raise ObligationProjectionError(
                "handle() requires the source topic in request['_topic']"
            )
        event = ModelObligationEventInbound(**request)
        result = self.project(event, db_raw, topic_raw)
        return result.model_dump(mode="json")


__all__: list[str] = ["HandlerProjectionOpenObligations"]

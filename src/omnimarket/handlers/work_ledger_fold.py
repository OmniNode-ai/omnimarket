# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The pure work-ledger fold (OMN-19513): one row in, its log record and state ops out.

No clock, no broker, no database. ``apply_ops`` is the in-memory twin of the
writer's guarded SQL, so the fold's order-independence is falsified by a unit
test and the parity check can fold the markdown file with the very same rules
the lab projection applies.

Entities (``work_ledger_state``):

* ``claim:<lane>``: a CLAIM opens it, a TERMINAL by the lane (or a RELEASE
  naming the claim's timestamp) closes it. One open claim per lane: a TERMINAL
  carries no ticket by grammar, so it can name only its lane.
* ``hold:<id>``: a HOLD opens it with its scope and ``until``; a RELEASE whose
  ``re=`` is the hold id closes it. ``until`` expiry is the reader's to judge
  against its own clock.
* ``msg:<id>``: a MSG opens it; an ACK whose ``re=`` is the id answers it. An
  ACK of a hold or a ruling makes a stub with no ``opened_at``, never open.
* ``ruling:<stamp>:<lane>`` and ``consent:<stamp>:<lane>``: opened by their row;
  closed only by a later RULING or OPERATOR-CONSENT that ``supersedes=`` it.

STATUS, FRICTION and CORRECTION rows open no entity.

Relationships (OMN-21107, TA.5): a RULING or OPERATOR-CONSENT row's
``supersedes=``, ``amends=`` and ``extends=`` cells, and a CORRECTION row's
``corrects=`` cell (with its ``operator_span=``), each fold to one immutable
link naming its target by the cell's value. A link is a function of the row's
text alone, so ``links_for_rows`` re-derives every link from the stored
``raw_row`` and no link table is written. ``close_relationships`` resolves
the links against the folded state by the target row's timestamp: a
``supersedes`` link closes its target, ``amends`` and ``extends`` link without
closing, and a ``corrects`` link lists the correcting row on its target of any
kind. A hold whose ``until`` is at or before ``as_of`` reads as expired.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from threading import Lock
from uuid import UUID

from omnibase_core.models.events.work.model_work_ledger_line import (
    dump_work_ledger_line,
)
from omnibase_core.models.events.work.model_work_ledger_record import (
    ModelWorkLedgerRecord,
)
from omnibase_core.models.nodes.work_ledger_state import (
    ModelWorkLedgerFoldInput,
    ModelWorkLedgerState,
)
from omnibase_core.models.nodes.work_ledger_state.model_work_ledger_projection_row import (
    ModelWorkLedgerProjectionRow,
)
from omnibase_core.nodes.node_work_ledger_state_compute.handler import (
    NodeWorkLedgerStateCompute,
)

from omnimarket.enums.enum_work_ledger_entity_kind import (
    EnumWorkLedgerEntityKind as Kind,
)
from omnimarket.enums.enum_work_ledger_entity_kind import (
    EnumWorkLedgerRelation as Relation,
)
from omnimarket.enums.enum_work_ledger_entity_kind import (
    EnumWorkLedgerStateOp as Op,
)
from omnimarket.events.enum_ledger_row_type import (
    EnumLedgerRowType as RowType,
)
from omnimarket.events.model_ledger_row_event import (
    ModelLedgerAckEvent,
    ModelLedgerClaimEvent,
    ModelLedgerCorrectionEvent,
    ModelLedgerHoldEvent,
    ModelLedgerMsgEvent,
    ModelLedgerOperatorConsentEvent,
    ModelLedgerReleaseEvent,
    ModelLedgerRulingEvent,
    ModelLedgerTerminalEvent,
)
from omnimarket.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.models.model_work_ledger_fold_result import (
    ModelWorkLedgerFoldResult,
    ModelWorkLedgerLink,
    ModelWorkLedgerRowRecord,
    ModelWorkLedgerStateOp,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    LedgerRowRefusalError,
    parse_ledger_row,
)

_BARE_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
# A row timestamp inside a relationship cell (``2026-10-02T11:25:17Z``,
# ``RULING 2026-...Z``, ``CLAIM@2026-...Z``), but not a hold or message id
# (``2026-...Z-lane``), which continues with a hyphen.
_STAMP_IN_REF = re.compile(
    r"(?<![0-9A-Za-z-])(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)(?![0-9A-Za-z-])"
)
_RELATION_FIELDS: tuple[Relation, ...] = (
    Relation.SUPERSEDES,
    Relation.AMENDS,
    Relation.EXTENDS,
)


class WorkLedgerFoldError(ValueError):
    """The row cannot be folded: it is not a canonical ledger row."""


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def target_stamp_of(ref: str) -> datetime | None:
    """The one row timestamp a relationship cell names, or None (a line number, prose)."""
    stamps = set(_STAMP_IN_REF.findall(ref))
    return parse_stamp(stamps.pop()) if len(stamps) == 1 else None


def _refs(values: tuple[str, ...]) -> tuple[str, ...]:
    """A cell may list several targets, comma-separated; each is one link."""
    return tuple(
        dict.fromkeys(
            part.strip()
            for value in values
            for part in value.split(",")
            if part.strip()
        )
    )


def fold_row(request: ModelWorkLedgerFoldRequest) -> ModelWorkLedgerFoldResult:
    """Fold one row. Pure and deterministic."""
    try:
        event = parse_ledger_row(
            request.raw_row, ledger_id=request.ledger_id, source=request.source
        )
    except LedgerRowRefusalError as exc:
        raise WorkLedgerFoldError(str(exc)) from exc

    at = parse_stamp(event.row_timestamp)
    lane = event.row_lane
    ticket = event.tickets[0] if event.tickets else None
    record = ModelWorkLedgerRowRecord(
        row_id=event.row_id,
        ledger_id=event.ledger_id,
        row_ts=at,
        row_type=event.row_type.value,
        row_lane=lane,
        tickets=event.tickets,
        raw_row=event.raw_row,
        source=event.source,
        ledger_seq=request.ledger_seq,
    )

    def op(key: str, kind: Kind, which: Op, **fields: object) -> ModelWorkLedgerStateOp:
        return ModelWorkLedgerStateOp(
            entity_key=key,
            kind=kind,
            op=which,
            at=at,
            row_id=event.row_id,
            **fields,
        )

    ops: list[ModelWorkLedgerStateOp] = []
    if isinstance(event, ModelLedgerClaimEvent) and lane:
        ops.append(
            op(
                f"claim:{lane}",
                Kind.CLAIM,
                Op.OPEN,
                lane=lane,
                ticket=ticket,
                repo=event.repo,
                pr=event.pr,
            )
        )
    elif isinstance(event, ModelLedgerTerminalEvent) and lane:
        ops.append(op(f"claim:{lane}", Kind.CLAIM, Op.CLOSE))
    elif isinstance(event, ModelLedgerHoldEvent) and event.hold_id:
        ops.append(
            op(
                f"hold:{event.hold_id}",
                Kind.HOLD,
                Op.OPEN,
                lane=lane,
                ticket=ticket,
                repo=event.scope_repo,
                pr=event.scope_pr,
                scope_to=event.scope_to,
                scope_surface=event.scope_surface,
                until_at=parse_stamp(event.until)
                if event.until and _BARE_STAMP.match(event.until)
                else None,
            )
        )
    elif isinstance(event, ModelLedgerReleaseEvent) and event.re:
        if _BARE_STAMP.match(event.re):
            if lane:  # a claim given up: re= is the claim's own timestamp
                ops.append(op(f"claim:{lane}", Kind.CLAIM, Op.CLOSE))
        else:
            ops.append(op(f"hold:{event.re}", Kind.HOLD, Op.CLOSE))
    elif isinstance(event, ModelLedgerMsgEvent) and event.msg_id:
        ops.append(
            op(
                f"msg:{event.msg_id}",
                Kind.MSG,
                Op.OPEN,
                lane=event.sender,
                ticket=ticket,
                repo=event.repo,
                pr=event.pr,
                scope_to=",".join(event.recipients) or None,
            )
        )
    elif isinstance(event, ModelLedgerAckEvent) and event.re:
        ops.append(op(f"msg:{event.re}", Kind.MSG, Op.CLOSE))
    elif isinstance(event, ModelLedgerRulingEvent) and lane:
        ops.append(
            op(
                f"ruling:{event.row_timestamp}:{lane}",
                Kind.RULING,
                Op.OPEN,
                lane=lane,
                ticket=ticket,
                detail=event.free_text[:500] or None,
            )
        )
    elif isinstance(event, ModelLedgerOperatorConsentEvent) and lane:
        ops.append(
            op(
                f"consent:{event.row_timestamp}:{lane}",
                Kind.CONSENT,
                Op.OPEN,
                lane=lane,
                ticket=ticket,
                detail=f"APPROVED SCOPE: {event.approved_scope} | OUT OF SCOPE: {event.out_of_scope}",
            )
        )
    links: list[ModelWorkLedgerLink] = []

    def link(
        relation: Relation, values: tuple[str, ...], operator_span: str | None = None
    ) -> None:
        source_key = ops[0].entity_key if ops else None
        for ref in _refs(values):
            links.append(
                ModelWorkLedgerLink(
                    source_row_id=event.row_id,
                    source_entity_key=source_key,
                    relation=relation,
                    target_ref=ref,
                    target_stamp=target_stamp_of(ref),
                    at=at,
                    operator_span=operator_span,
                )
            )

    if isinstance(event, ModelLedgerRulingEvent | ModelLedgerOperatorConsentEvent):
        for relation in _RELATION_FIELDS:
            link(relation, getattr(event, relation.value))
    elif isinstance(event, ModelLedgerCorrectionEvent):
        link(Relation.CORRECTS, event.corrects, event.operator_span)
    _ = RowType  # the enum is the closed set the parser already enforced
    return ModelWorkLedgerFoldResult(row=record, ops=tuple(ops), links=tuple(links))


class WorkLedgerFoldMemo:
    """Share pure row folds across sources, retaining one read's keys (OMN-21099)."""

    def __init__(
        self,
        fold: Callable[
            [ModelWorkLedgerFoldRequest], ModelWorkLedgerFoldResult
        ] = fold_row,
    ) -> None:
        self._fold = fold
        self._cache: dict[str, tuple[str, ModelWorkLedgerFoldResult | None]] = {}
        self._lock = Lock()

    def fold_rows(
        self, rows: Sequence[tuple[str, str]]
    ) -> tuple[ModelWorkLedgerFoldResult | None, ...]:
        """Fold changed rows in order; reuse results and refusals for unchanged rows."""
        with self._lock:
            retained: dict[str, tuple[str, ModelWorkLedgerFoldResult | None]] = {}
            results: list[ModelWorkLedgerFoldResult | None] = []
            for key, raw_row in rows:
                cached = retained.get(key, self._cache.get(key))
                if cached is not None and cached[0] == raw_row:
                    result = cached[1]
                else:
                    try:
                        result = self._fold(ModelWorkLedgerFoldRequest(raw_row=raw_row))
                    except WorkLedgerFoldError:
                        result = None
                retained[key] = (raw_row, result)
                results.append(result)
            self._cache = retained
            return tuple(results)


SHARED_WORK_LEDGER_FOLD_MEMO = WorkLedgerFoldMemo()


def _may_overwrite(
    current_at: object, current_row_id: object, incoming: tuple[datetime, str]
) -> bool:
    """The SQL guard ``current IS NULL OR (at, row_id) <= EXCLUDED``."""
    if not isinstance(current_at, datetime) or not isinstance(current_row_id, str):
        return True
    return (current_at, current_row_id) <= incoming


def apply_ops(
    state: dict[str, dict[str, object]], ops: tuple[ModelWorkLedgerStateOp, ...]
) -> None:
    """Apply ops to an in-memory state exactly as the writer's guarded upserts do."""
    for o in ops:
        row = state.setdefault(
            o.entity_key,
            {
                "kind": o.kind.value,
                "lane": None,
                "ticket": None,
                "repo": None,
                "pr": None,
                "scope_to": None,
                "scope_surface": None,
                "until_at": None,
                "detail": None,
                "opened_at": None,
                "opened_row_id": None,
                "closed_at": None,
                "closed_row_id": None,
                "is_open": False,
            },
        )
        incoming = (o.at, o.row_id)
        if o.op is Op.OPEN:
            if _may_overwrite(row["opened_at"], row["opened_row_id"], incoming):
                row.update(
                    lane=o.lane,
                    ticket=o.ticket,
                    repo=o.repo,
                    pr=o.pr,
                    scope_to=o.scope_to,
                    scope_surface=o.scope_surface,
                    until_at=o.until_at,
                    detail=o.detail,
                    opened_at=o.at,
                    opened_row_id=o.row_id,
                )
        else:
            if _may_overwrite(row["closed_at"], row["closed_row_id"], incoming):
                row.update(closed_at=o.at, closed_row_id=o.row_id)
        opened, closed = row["opened_at"], row["closed_at"]
        row["is_open"] = isinstance(opened, datetime) and (
            not isinstance(closed, datetime) or closed < opened
        )


def links_for_rows(raw_rows: Iterable[str]) -> tuple[ModelWorkLedgerLink, ...]:
    """Every link the rows carry, re-derived by the same fold. Rows that are not
    canonical ledger rows carry none."""
    links: list[ModelWorkLedgerLink] = []
    for raw in raw_rows:
        try:
            links.extend(fold_row(ModelWorkLedgerFoldRequest(raw_row=raw)).links)
        except WorkLedgerFoldError:
            continue
    return tuple(links)


def close_relationships(
    state: dict[str, dict[str, object]],
    links: Iterable[ModelWorkLedgerLink],
    *,
    as_of: datetime | None = None,
) -> tuple[dict[str, dict[str, object]], tuple[ModelWorkLedgerLink, ...]]:
    """Resolve links against folded state. Pure, and independent of arrival order.

    Returns each entity with its incoming links (``superseded_by``,
    ``amended_by``, ``extended_by``, ``corrected_by`` as sorted row ids, and
    ``operator_spans`` as sorted ``(row_id, span)`` pairs), ``expired`` and the
    closed ``is_open``; and the links whose target names no folded entity. A
    target is every entity opened at the named timestamp other than the
    linking row's own; a ``corrects`` target with none falls back to every
    entity closed at it (a corrected TERMINAL or RELEASE names its claim or hold).
    """
    unique = {(lk.source_row_id, lk.relation, lk.target_ref): lk for lk in links}
    opened: dict[datetime, list[str]] = {}
    closed: dict[datetime, list[str]] = {}
    for key, entity in state.items():
        if isinstance(entity["opened_at"], datetime):
            opened.setdefault(entity["opened_at"], []).append(key)
        if isinstance(entity["closed_at"], datetime):
            closed.setdefault(entity["closed_at"], []).append(key)

    incoming: dict[str, dict[Relation, set[str]]] = {}
    spans: dict[str, set[tuple[str, str]]] = {}
    unresolved: list[ModelWorkLedgerLink] = []
    for lk in unique.values():
        stamp = lk.target_stamp
        targets = [
            k
            for k in (opened.get(stamp, []) if stamp else [])
            if k != lk.source_entity_key
        ]
        if not targets and stamp and lk.relation is Relation.CORRECTS:
            targets = list(closed.get(stamp, []))
        if not targets:
            unresolved.append(lk)
            continue
        for key in targets:
            incoming.setdefault(key, {}).setdefault(lk.relation, set()).add(
                lk.source_row_id
            )
            if lk.relation is Relation.CORRECTS and lk.operator_span:
                spans.setdefault(key, set()).add((lk.source_row_id, lk.operator_span))

    closure: dict[str, dict[str, object]] = {}
    for key, entity in state.items():
        rel = incoming.get(key, {})
        until = entity["until_at"]
        expired = as_of is not None and isinstance(until, datetime) and until <= as_of
        superseded = bool(rel.get(Relation.SUPERSEDES))
        closure[key] = {
            **entity,
            "superseded_by": tuple(sorted(rel.get(Relation.SUPERSEDES, ()))),
            "amended_by": tuple(sorted(rel.get(Relation.AMENDS, ()))),
            "extended_by": tuple(sorted(rel.get(Relation.EXTENDS, ()))),
            "corrected_by": tuple(sorted(rel.get(Relation.CORRECTS, ()))),
            "operator_spans": tuple(sorted(spans.get(key, ()))),
            "expired": expired,
            "is_open": bool(entity["is_open"]) and not superseded and not expired,
        }
    unresolved.sort(key=lambda lk: (lk.source_row_id, lk.relation.value, lk.target_ref))
    return closure, tuple(unresolved)


__all__: list[str] = [
    "SHARED_WORK_LEDGER_FOLD_MEMO",
    "WorkLedgerFoldError",
    "WorkLedgerFoldMemo",
    "apply_ops",
    "close_relationships",
    "fold_row",
    "links_for_rows",
    "parse_stamp",
    "target_stamp_of",
]


def rows_for_records(
    records: Iterable[ModelWorkLedgerRecord],
) -> tuple[ModelWorkLedgerProjectionRow, ...]:
    """Canonicalize identities, refusing conflicting immutable records."""
    by_id: dict[UUID, str] = {}
    for record in records:
        canonical = dump_work_ledger_line(record)
        event_id = record.event.event_id
        existing = by_id.get(event_id)
        if existing is not None and existing != canonical:
            raise WorkLedgerFoldError(
                f"conflicting canonical content for event_id {event_id}"
            )
        by_id[event_id] = canonical
    return tuple(
        ModelWorkLedgerProjectionRow(event_id=event_id, record=canonical)
        for event_id, canonical in sorted(by_id.items(), key=lambda item: str(item[0]))
    )


def fold_records(
    records: Iterable[ModelWorkLedgerRecord], *, as_of: datetime | None = None
) -> ModelWorkLedgerState:
    """Fold typed records with the released core reducer, without reinterpretation."""
    materialized = tuple(records)
    rows_for_records(materialized)
    return NodeWorkLedgerStateCompute().handle(
        ModelWorkLedgerFoldInput(
            lines=tuple(dump_work_ledger_line(record) for record in materialized),
            as_of=as_of,
        )
    )

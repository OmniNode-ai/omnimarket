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
* ``ruling:<stamp>:<lane>`` and ``consent:<stamp>:<lane>``: opened by their row
  and never closed.

STATUS, FRICTION and CORRECTION rows are logged and open no entity.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from omnimarket.events.enum_ledger_row_type import (
    EnumLedgerRowType as RowType,
)
from omnimarket.events.model_ledger_row_event import (
    ModelLedgerAckEvent,
    ModelLedgerClaimEvent,
    ModelLedgerHoldEvent,
    ModelLedgerMsgEvent,
    ModelLedgerOperatorConsentEvent,
    ModelLedgerReleaseEvent,
    ModelLedgerRulingEvent,
    ModelLedgerTerminalEvent,
)
from omnimarket.nodes.node_projection_work_ledger.models.enum_work_ledger_entity_kind import (
    EnumWorkLedgerEntityKind as Kind,
)
from omnimarket.nodes.node_projection_work_ledger.models.enum_work_ledger_entity_kind import (
    EnumWorkLedgerStateOp as Op,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_fold_result import (
    ModelWorkLedgerFoldResult,
    ModelWorkLedgerRowRecord,
    ModelWorkLedgerStateOp,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    LedgerRowRefusalError,
    parse_ledger_row,
)

_BARE_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class WorkLedgerFoldError(ValueError):
    """The row cannot be folded: it is not a canonical ledger row."""


def parse_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


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
    _ = RowType  # the enum is the closed set the parser already enforced
    return ModelWorkLedgerFoldResult(row=record, ops=tuple(ops))


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


__all__: list[str] = ["WorkLedgerFoldError", "apply_ops", "fold_row", "parse_stamp"]

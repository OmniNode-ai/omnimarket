# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Render a page of ``work_ledger_state`` as the answer of a point query (OMN-21948).

The state table selects the open set; each entity's opening row is rendered with
the markdown ledger's own read rules (``work_ledger_query_replay``) so the typed
sections and the ``lines`` match what the ``onex-ledger`` verbs print. Where the
file prints ``L<n>``, a position that does not exist in the log, the line ends
with ``db:<row_id>`` instead.

A page that cannot be rendered whole (an opening row that is not a ledger row, a
hold the rules drop) is a read error, never a shorter page.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerHoldCounts,
    ModelWorkLedgerOpenClaim,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerStateCursor,
    ModelWorkLedgerStateEntity,
    ModelWorkLedgerStatePage,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_FREE_TEXT_WIDTH = 160


def stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_STAMP_FORMAT)


def parse_claims_since(value: str) -> datetime:
    try:
        return datetime.strptime(value, _STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise WorkLedgerReadError(
            f"claims_since {value!r} is not a ledger timestamp"
        ) from None


def encode_cursor(
    point: EnumWorkLedgerQueryKind, entity: ModelWorkLedgerStateEntity
) -> str:
    """Opaque cursor naming the last entity of a page and the query it belongs to."""
    body = json.dumps(
        {"q": point.value, "o": entity.opened_at.isoformat(), "k": entity.entity_key},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


def decode_cursor(
    cursor: str, point: EnumWorkLedgerQueryKind
) -> ModelWorkLedgerStateCursor:
    try:
        body = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if body["q"] != point.value:
            raise WorkLedgerReadError(f"cursor belongs to query {body['q']!r}")
        return ModelWorkLedgerStateCursor(
            opened_at=datetime.fromisoformat(body["o"]), entity_key=body["k"]
        )
    except WorkLedgerReadError:
        raise
    except (binascii.Error, ValueError, KeyError, TypeError, ValidationError):
        raise WorkLedgerReadError("cursor is not a page cursor of this query") from None


def _parsed(entities: Sequence[ModelWorkLedgerStateEntity]) -> list[replay.LedgerRow]:
    """One ledger row per entity, in order; an entity whose row does not parse is an error."""
    rows = replay.parse_rows(e.opened_row for e in entities)
    if len(rows) != len(entities):
        known = {r.record.row_id for r in rows}
        lost = next(e for e in entities if e.opened_row.row_id not in known)
        raise WorkLedgerReadError(
            f"state entity {lost.entity_key}: opening row {lost.opened_row.row_id} "
            "is not a ledger row"
        )
    return rows


def _age(now: datetime, moment: datetime) -> str:
    seconds = max(0, int((now - moment).total_seconds()))
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _hold_lines(
    request: ModelWorkLedgerQueryRequest,
    entities: Sequence[ModelWorkLedgerStateEntity],
    page: ModelWorkLedgerStatePage,
    now: datetime,
) -> dict[str, object]:
    rows = _parsed(entities)
    holds, _ = replay.holds_in_force(rows, now=now)
    if len(holds) != len(entities):
        raise WorkLedgerReadError(
            f"state page holds {len(entities)} open holds but {len(holds)} read as in force"
        )
    counts = ModelWorkLedgerHoldCounts(
        checked=page.total, released=page.released, expired=page.expired
    )
    lines = [
        f"HOLD {h.hold_id} by={h.by} scope=<"
        + ";".join(
            f"{key}={value}"
            for key, value in (
                ("to", h.to),
                ("repo", h.repo),
                ("pr", h.pr),
                ("surface", h.surface),
            )
            if value
        )
        + f"> until={h.until or 'open'} db:{h.row.row_id}"
        for h in holds
    ] or [
        f"HOLD none checked={counts.checked} released={counts.released} "
        f"expired={counts.expired} legacy-no-id={counts.legacy_no_id}"
    ]
    return {
        "matched": len(holds),
        "holds": tuple(holds),
        "hold_counts": counts,
        "lines": tuple(lines),
    }


def _free_text(row: replay.LedgerRow) -> str:
    free = [c for c in row.cells if not replay.FIELD_RE.match(c)]
    return max(free, key=len) if free else ""


def _inbox_lines(
    request: ModelWorkLedgerQueryRequest,
    entities: Sequence[ModelWorkLedgerStateEntity],
) -> dict[str, object]:
    rows = _parsed(entities)
    by_id = {r.record.row_id: r for r in rows}
    lanes = request.lanes or ((request.lane,) if request.lane else ())
    boxes = tuple(replay.inbox(rows, lane) for lane in lanes)
    lines: list[str] = []
    for box in boxes:
        opened = [by_id[e.row.row_id] for e in box.entries]
        action = [by_id[e.row.row_id] for e in box.entries if e.needs_action]
        holds = sum(r.row_type == "HOLD" for r in action)
        newest = opened[-1].stamp if opened else "none"
        lines.append(
            f"inbox {box.lane}: {len(opened)} open, {len(action)} need action "
            f"(HOLD:{holds} ask:{len(action) - holds}), "
            f"{len(opened) - len(action)} FYI, newest={newest}"
        )
        lines += [
            f"  {r.row_type} id={r.token('id')} "
            f"from={r.token('from') or r.token('lane')} db:{r.record.row_id}: "
            f"{_free_text(r)[:_FREE_TEXT_WIDTH]}"
            for r in action
        ]
    return {
        "matched": sum(len(b.entries) for b in boxes),
        "inbox": boxes,
        "lines": tuple(lines),
    }


def _claim_lines(
    request: ModelWorkLedgerQueryRequest,
    entities: Sequence[ModelWorkLedgerStateEntity],
    page: ModelWorkLedgerStatePage,
    now: datetime,
) -> dict[str, object]:
    rows = _parsed(entities)
    newest_ledger = stamp(page.ledger_newest) if page.ledger_newest else "none"
    lines = [
        f"open-claims since={request.claims_since or 'beginning'} claims={page.total} "
        f"open={len(entities)} ledger-newest={newest_ledger} now={stamp(now)}"
    ]
    claims: list[ModelWorkLedgerOpenClaim] = []
    for entity, row in zip(entities, rows, strict=True):
        lane = entity.lane or row.cell("lane")
        activity = entity.activity
        newest_ts = activity.newest_row_ts if activity else row.record.row_ts
        newest_kind = activity.newest_row_kind if activity else row.row_type
        rows_since = activity.rows if activity else 1
        leases = list(activity.leases) if activity else []
        if (
            lane.startswith("landing-")
            and len(lane) > len("landing-")
            and lane not in leases
        ):
            leases.append(lane)
        ticket = row.cell("ticket") or row.cell("tickets") or "none"
        host = row.cell("host") or "none"
        run = row.cell("run") or "none"
        claims.append(
            ModelWorkLedgerOpenClaim(
                lane=lane,
                claim=row.record,
                ticket=ticket,
                newest_row_stamp=stamp(newest_ts),
                newest_row_kind=newest_kind,
                rows=rows_since,
                host=host,
                run=run,
                leases=tuple(leases),
            )
        )
        lines.append(
            f"OPEN-CLAIM lane={lane} claim={row.stamp} db:{row.record.row_id} "
            f"ticket={ticket} newest-row={stamp(newest_ts)} kind={newest_kind} "
            f"age={_age(now, newest_ts)} rows={rows_since} host={host} run={run} "
            f"leases={','.join(leases) or 'none'} liveness=unknown"
        )
    return {"matched": len(claims), "open_claims": tuple(claims), "lines": tuple(lines)}


def _ruling_lines(
    request: ModelWorkLedgerQueryRequest,
    entities: Sequence[ModelWorkLedgerStateEntity],
) -> dict[str, object]:
    rows = _parsed(entities)
    stamps = ",".join(r.stamp for r in rows) or "none"
    return {
        "matched": len(rows),
        "rows": tuple(r.record for r in rows),
        "lines": (
            f"OPEN-RULINGS ticket={request.ticket} n={len(rows)} stamps={stamps}",
        ),
    }


def state_answer(
    request: ModelWorkLedgerQueryRequest,
    page: ModelWorkLedgerStatePage,
    now: datetime,
) -> dict[str, object]:
    """The answer sections and verb lines of one state page."""
    kind = request.query
    if kind is EnumWorkLedgerQueryKind.HOLDS:
        return _hold_lines(request, page.entities, page, now)
    if kind is EnumWorkLedgerQueryKind.INBOX:
        return _inbox_lines(request, page.entities)
    if kind is EnumWorkLedgerQueryKind.OPEN_CLAIMS:
        return _claim_lines(request, page.entities, page, now)
    return _ruling_lines(request, page.entities)


__all__ = [
    "decode_cursor",
    "encode_cursor",
    "parse_claims_since",
    "state_answer",
]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The item fold: ledger rows and plan-owner MISSING rules in, one typed item per source event out (OMN-25981).

Pure: the rows, the plan rules, the evaluating time and the policy are inputs; no I/O and no clock. Four sources:

(a) a lane TERMINAL whose outcome is one of ``item_outcomes`` and that no later CLAIM for its ticket or PR follows;
(b) a RULING carrying the pending-mechanism cell until a CORRECTION ``corrects=<its stamp>`` names the mechanism;
(c) a MISSING verdict of the plan owner check, first seen at the node's own ``kind=item`` row for it (or now);
(d) an operator-capture ask (the capture's own ``item=cap-<hex>`` id) until a row moves it to a closed state.

Ownership is read from the same rows: the node's ``state=owned owner=<lane> rev=<rev> gen=<n>`` transitions (a
transition of another revision is ignored, the fence of plan task 3.3), the owner lane's CLAIM and TERMINAL, and any
live CLAIM whose ``cause=`` cell names the item. A TERMINAL of the owner lane ends that binding: the item keeps its
accountable owner and has no running lane, so the owner step gives it the next generation's lane.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from omnimarket.handlers.ledger_row_grammar import Row, parse_row
from omnimarket.nodes.node_item_owner_dispatch_effect.models import (
    EnumItemSource,
    EnumItemState,
    ModelItemFold,
    ModelItemRecord,
)
from omnimarket.nodes.node_item_owner_dispatch_effect.overlay import (
    ModelItemOwnerPolicy,
)

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_TYPES = frozenset({"CLAIM", "TERMINAL", "RULING", "CORRECTION", "STATUS"})


def moment(stamp: str) -> datetime:
    return datetime.strptime(stamp, STAMP_FORMAT).replace(tzinfo=UTC)


def stamp_of(value: datetime) -> str:
    return value.astimezone(UTC).strftime(STAMP_FORMAT)


def _hex(text: str, size: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:size]


def item_id_for(source: EnumItemSource, ref: str) -> str:
    """The deterministic id: the same source event always folds to the same id."""
    return f"itm-{_hex(f'{source.value}|{ref}', 10)}"


def _summary(row: Row, limit: int) -> str:
    tail = row.cells[-1] if row.cells else ""
    return " ".join(tail.replace("|", " ").split())[:limit]


@dataclass
class _Seed:
    item_id: str
    source: EnumItemSource
    ref: str
    first_seen: str
    revision: str
    ticket: str | None = None
    pr: str | None = None
    summary: str = ""
    done: bool = False


@dataclass
class _Index:
    claims_by_lane: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    terminals_by_lane: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    claims_by_ticket: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    claims_by_pr: dict[str, list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    claims_by_cause: dict[str, list[tuple[str, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    owned: dict[str, list[tuple[str, str, str]]] = field(
        default_factory=lambda: defaultdict(list)
    )
    closed: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    item_rows: dict[str, str] = field(default_factory=dict)
    corrected: set[str] = field(default_factory=set)
    owner_lanes: set[str] = field(default_factory=set)


def _rows(lines: tuple[str, ...]) -> list[Row]:
    parsed: list[Row] = []
    for line in dict.fromkeys(line.strip() for line in lines if line.strip()):
        row = parse_row(line, types=_TYPES)
        if row is None:
            continue
        try:
            moment(row.stamp)
        except ValueError:
            continue
        parsed.append(row)
    parsed.sort(key=lambda row: row.stamp)
    return parsed


def _index(rows: list[Row], policy: ModelItemOwnerPolicy) -> _Index:
    index = _Index()
    closed_states = frozenset((*policy.capture_closed_states, "done"))
    for row in rows:
        lane = row.token("lane") or ""
        if row.row_type == "CLAIM":
            if lane:
                index.claims_by_lane[lane].append(row.stamp)
            for ticket in row.fields.get("ticket", []):
                index.claims_by_ticket[ticket].append(row.stamp)
            for pr in row.fields.get("pr", []):
                index.claims_by_pr[pr].append(row.stamp)
            for cause in row.fields.get("cause", []):
                index.claims_by_cause[cause].append((row.stamp, lane))
        elif row.row_type == "TERMINAL":
            if lane:
                index.terminals_by_lane[lane].append(row.stamp)
        elif row.row_type == "CORRECTION":
            corrects = row.value("corrects")
            if corrects and row.value(policy.ruling_done_cell):
                index.corrected.add(corrects)
        elif row.row_type == "STATUS":
            item = row.value("item")
            state = row.value("state")
            if not item:
                continue
            if lane == policy.node_lane and state == "owned":
                owner = row.value("owner")
                revision = row.value("rev")
                if owner and revision:
                    index.owned[item].append((row.stamp, owner, revision))
                    index.owner_lanes.add(owner)
            elif lane == policy.node_lane and row.value("kind") == "item":
                index.item_rows.setdefault(item, row.stamp)
            elif state in closed_states:
                index.closed[item].append(row.stamp)
    return index


def _seeds(
    rows: list[Row],
    plan_missing: tuple[str, ...],
    index: _Index,
    policy: ModelItemOwnerPolicy,
    now: str,
) -> list[_Seed]:
    seeds: dict[str, _Seed] = {}
    outcomes = frozenset(policy.item_outcomes)
    umbrella = frozenset(policy.umbrella_tickets)
    owner_stem = f"{policy.lane_prefix}-"
    for row in rows:
        lane = row.token("lane") or ""
        if row.row_type == "TERMINAL" and row.value("outcome") in outcomes:
            if not lane or lane in index.owner_lanes or lane.startswith(owner_stem):
                continue
            ref = f"{row.stamp} {lane}"
            ticket = row.value("ticket")
            pr = row.value("pr")
            succeeded = (
                ticket is not None
                and ticket not in umbrella
                and any(s > row.stamp for s in index.claims_by_ticket.get(ticket, ()))
            ) or (
                pr is not None
                and any(s > row.stamp for s in index.claims_by_pr.get(pr, ()))
            )
            item_id = item_id_for(EnumItemSource.BLOCKED_LANE, ref)
            seeds.setdefault(
                item_id,
                _Seed(
                    item_id,
                    EnumItemSource.BLOCKED_LANE,
                    ref,
                    row.stamp,
                    _hex(row.text.strip(), 12),
                    ticket=ticket,
                    pr=pr,
                    summary=_summary(row, policy.summary_limit),
                    done=succeeded,
                ),
            )
        elif row.row_type == "RULING" and row.value(policy.ruling_pending_cell):
            ref = f"{row.stamp} {lane}"
            item_id = item_id_for(EnumItemSource.RULING, ref)
            seeds.setdefault(
                item_id,
                _Seed(
                    item_id,
                    EnumItemSource.RULING,
                    ref,
                    row.stamp,
                    _hex(row.text.strip(), 12),
                    ticket=row.value("ticket"),
                    summary=_summary(row, policy.summary_limit),
                    done=row.stamp in index.corrected
                    or bool(row.value(policy.ruling_done_cell)),
                ),
            )
        elif (
            row.row_type == "STATUS"
            and lane == policy.capture_lane
            and row.value("state") in policy.capture_open_states
        ):
            item_id = row.value("item") or ""
            if not (item_id.startswith("cap-") and len(item_id) == 14):
                continue
            seeds.setdefault(
                item_id,
                _Seed(
                    item_id,
                    EnumItemSource.OPERATOR_CAPTURE,
                    f"{row.stamp} capture={row.value('capture') or item_id}",
                    row.stamp,
                    _hex(row.text.strip(), 12),
                    summary=_summary(row, policy.summary_limit),
                ),
            )
    for rule in dict.fromkeys(rule.strip() for rule in plan_missing if rule.strip()):
        item_id = item_id_for(EnumItemSource.PLAN_OWNER, rule)
        seeds.setdefault(
            item_id,
            _Seed(
                item_id,
                EnumItemSource.PLAN_OWNER,
                rule,
                index.item_rows.get(item_id, now),
                _hex(f"plan-owner|{rule}", 12),
                summary=f"plan owner check MISSING {rule}",
            ),
        )
    for seed in seeds.values():
        if any(
            stamp >= seed.first_seen for stamp in index.closed.get(seed.item_id, ())
        ):
            seed.done = True
    return list(seeds.values())


def _live_cause_lane(
    item_id: str, index: _Index, policy: ModelItemOwnerPolicy
) -> tuple[str, str] | None:
    """A lane whose CLAIM names this item's cause and that has written no TERMINAL since: (claim stamp, lane)."""
    for stamp, lane in reversed(
        index.claims_by_cause.get(f"{policy.cause_prefix}:{item_id}", [])
    ):
        if lane and not any(t >= stamp for t in index.terminals_by_lane.get(lane, ())):
            return stamp, lane
    return None


def _record(
    seed: _Seed, index: _Index, policy: ModelItemOwnerPolicy, now: datetime
) -> ModelItemRecord:
    bindings = [b for b in index.owned.get(seed.item_id, []) if b[2] == seed.revision]
    accountable: str | None = None
    running: str | None = None
    unowned_since = seed.first_seen
    if bindings:
        bound_at, owner, _ = bindings[-1]
        accountable = owner
        ended = [t for t in index.terminals_by_lane.get(owner, []) if t >= bound_at]
        if ended:
            unowned_since = ended[0]
        elif index.claims_by_lane.get(owner) or now - moment(bound_at) < timedelta(
            minutes=policy.settle_minutes
        ):
            running = owner
        else:
            # Placed but never claimed inside the settle window: the binding is orphaned and the item open again.
            unowned_since = bound_at
    live = _live_cause_lane(seed.item_id, index, policy)
    if running is None and live is not None:
        running = live[1]
        accountable = accountable or live[1]
    if seed.done:
        state = EnumItemState.DONE
    elif running is not None:
        state = EnumItemState.OWNED
    else:
        state = EnumItemState.OPEN
    return ModelItemRecord(
        item_id=seed.item_id,
        source=seed.source,
        source_ref=seed.ref,
        first_seen_at=seed.first_seen,
        revision=seed.revision,
        state=state,
        accountable_owner=accountable,
        running_lane=running,
        generation=len(bindings),
        unowned_since=unowned_since,
        ticket=seed.ticket,
        pr=seed.pr,
        summary=seed.summary,
    )


def fold_items(
    rows: tuple[str, ...],
    plan_missing: tuple[str, ...],
    now: str,
    policy: ModelItemOwnerPolicy,
) -> ModelItemFold:
    """Every item the rows and the plan rules hold, with its owner state at ``now``."""
    parsed = _rows(rows)
    index = _index(parsed, policy)
    at = moment(now)
    items = tuple(
        _record(seed, index, policy, at)
        for seed in _seeds(parsed, plan_missing, index, policy, now)
    )
    return ModelItemFold(items=items, rows_read=len(parsed))


__all__ = ["fold_items", "item_id_for", "moment", "stamp_of"]

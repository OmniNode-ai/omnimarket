# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive a deterministic factory floor from caller-supplied snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_factory_floor_input import (
    ItemState,
    ModelFactoryFloorConfig,
    ModelFactoryFloorRequest,
    ModelFloorLedgerRow,
    ModelFloorManifestTask,
    ModelFloorPlanInput,
    ModelFloorTableTask,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_containers import (
    with_application_containers,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_lanes import (
    build_cloud,
    build_lanes,
    build_machines,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFactoryFloorResult,
    ModelFloorAndon,
    ModelFloorBay,
    ModelFloorItem,
    ModelFloorLine,
    ModelFloorPrRef,
    ModelFloorSection,
    ModelFloorSources,
    ModelFloorStation,
    ModelFloorStoppageOut,
)


@dataclass(frozen=True)
class _Index:
    """What the per-task rules look up in the ledger, read once per request (a scan per task and hold is quadratic)."""

    released_prs: dict[str, frozenset[str]]
    lane_newest: dict[str, datetime]
    lane_terminals: dict[str, tuple[datetime, ...]]
    # Ledger positions by task cell, by ticket, and by a task id found anywhere in the row's reason or text.
    by_task: dict[str, tuple[int, ...]]
    by_ticket: dict[str, tuple[int, ...]]
    by_mention: dict[str, tuple[int, ...]]
    # Per ticket, the newest CLAIM and the newest CLAIM by any other lane: (ts, lane, other-lane ts).
    claim_tips: dict[str, tuple[datetime, str, datetime | None]]


def _tip(
    held: tuple[datetime, str, datetime | None] | None, row: ModelFloorLedgerRow
) -> tuple[datetime, str, datetime | None]:
    if held is None:
        return row.ts, row.lane, None
    ts, lane, other = held
    if row.lane == lane:
        return max(ts, row.ts), lane, other
    if row.ts > ts:
        return row.ts, row.lane, ts if other is None else max(ts, other)
    return ts, lane, row.ts if other is None else max(row.ts, other)


def _task_ids(plans: tuple[ModelFloorPlanInput, ...]) -> frozenset[str]:
    ids = {row.label for plan in plans for row in plan.table_tasks}
    ids.update(
        task.id
        for plan in plans
        if plan.manifest is not None
        for task in plan.manifest.tasks
    )
    return frozenset(ids)


def _index(request: ModelFactoryFloorRequest, task_ids: frozenset[str]) -> _Index:
    released: dict[str, set[str]] = {}
    newest: dict[str, datetime] = {}
    terminals: dict[str, list[datetime]] = {}
    by_task: dict[str, list[int]] = {}
    by_ticket: dict[str, list[int]] = {}
    by_mention: dict[str, list[int]] = {}
    tips: dict[str, tuple[datetime, str, datetime | None]] = {}
    for position, row in enumerate(request.ledger):
        if row.kind == "CLAIM":
            for ticket in row.tickets:
                tips[ticket] = _tip(tips.get(ticket), row)
        if row.task:
            by_task.setdefault(row.task, []).append(position)
        for ticket in row.tickets:
            by_ticket.setdefault(ticket, []).append(position)
        for task_id in task_ids:
            if task_id in row.reason or task_id in row.text:
                by_mention.setdefault(task_id, []).append(position)
        if row.kind == "RELEASE":
            released.setdefault(row.re, set()).update(row.prs)
        newest[row.lane] = max(newest.get(row.lane, row.ts), row.ts)
        if row.kind == "TERMINAL":
            terminals.setdefault(row.lane, []).append(row.ts)
    return _Index(
        released_prs={key: frozenset(value) for key, value in released.items()},
        lane_newest=newest,
        lane_terminals={key: tuple(value) for key, value in terminals.items()},
        by_task={key: tuple(value) for key, value in by_task.items()},
        by_ticket={key: tuple(value) for key, value in by_ticket.items()},
        by_mention={key: tuple(value) for key, value in by_mention.items()},
        claim_tips=tips,
    )


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _token(task_id: str, text: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(task_id)}(?![\w.])", text) is not None


def _intersects(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return bool(set(left).intersection(right))


def _scope(plan: ModelFloorPlanInput) -> tuple[str, ...]:
    manifest = plan.manifest
    if manifest is None:
        return tuple(dict.fromkeys(plan.tickets))
    epic = (manifest.epic,) if manifest.epic else ()
    tickets = tuple(ticket for task in manifest.tasks for ticket in task.tickets)
    return tuple(dict.fromkeys(plan.tickets + epic + tickets))


def _row_key(row: ModelFloorLedgerRow) -> tuple[datetime, str, str, str, str]:
    return row.ts, row.row_id, row.lane, row.outcome, row.verdict


def _holds(request: ModelFactoryFloorRequest) -> tuple[ModelFloorLedgerRow, ...]:
    released = {
        row.re for row in request.ledger if row.kind == "RELEASE" and not row.prs
    }
    return tuple(
        sorted(
            (
                row
                for row in request.ledger
                if row.kind == "HOLD"
                and row.row_id not in released
                and (row.until is None or row.until > request.generated_at)
            ),
            key=_row_key,
        )
    )


def _andon(row: ModelFloorLedgerRow, config: ModelFactoryFloorConfig) -> bool:
    return any(
        marker.casefold() in row.row_id.casefold()
        or marker.casefold() in row.reason.casefold()
        for marker in config.andon_markers
    )


def _hold_matches(
    hold: ModelFloorLedgerRow,
    task: ModelFloorManifestTask,
    prs: tuple[str, ...],
    index: _Index,
) -> bool:
    released_prs = index.released_prs.get(hold.row_id, frozenset())
    return (
        (not task.scoped and (hold.task == task.id or _token(task.id, hold.reason)))
        or _intersects(hold.tickets, task.tickets)
        or any(pr in hold.prs and pr not in released_prs for pr in prs)
    )


def _task_prs(
    task: ModelFloorManifestTask, request: ModelFactoryFloorRequest
) -> tuple[str, ...]:
    inferred = tuple(
        pr.key
        for pr in request.watcher.prs
        if (
            not task.scoped
            and (_token(task.id, pr.title) or _token(task.id, pr.head_ref))
        )
        or _intersects(task.tickets, pr.tickets)
    )
    return tuple(sorted(set(task.prs + inferred)))


def _task_rows(
    task: ModelFloorManifestTask,
    scope: tuple[str, ...],
    request: ModelFactoryFloorRequest,
    index: _Index,
) -> tuple[ModelFloorLedgerRow, ...]:
    by_ticket = (p for ticket in task.tickets for p in index.by_ticket.get(ticket, ()))
    if task.scoped:
        return tuple(request.ledger[position] for position in sorted(set(by_ticket)))
    candidates = {
        *index.by_task.get(task.id, ()),
        *index.by_mention.get(task.id, ()),
        *by_ticket,
    }
    return tuple(
        row
        for row in (request.ledger[position] for position in sorted(candidates))
        if row.task == task.id
        or _token(task.id, row.reason)
        or _intersects(row.tickets, task.tickets)
        or (_token(task.id, row.text) and _intersects(row.tickets, scope))
    )


def _live_claim(
    rows: tuple[ModelFloorLedgerRow, ...],
    request: ModelFactoryFloorRequest,
    index: _Index,
) -> ModelFloorLedgerRow | None:
    def live(row: ModelFloorLedgerRow) -> bool:
        closed = any(ts >= row.ts for ts in index.lane_terminals.get(row.lane, ()))
        newest = index.lane_newest[row.lane]
        return not closed and request.generated_at - newest <= timedelta(
            minutes=request.config.live_claim_minutes
        )

    return max(
        (row for row in rows if row.kind == "CLAIM" and live(row)),
        key=_row_key,
        default=None,
    )


def _evidence(
    task: ModelFloorManifestTask,
    prs: tuple[str, ...],
    scope: tuple[str, ...],
    holds: tuple[ModelFloorLedgerRow, ...],
    request: ModelFactoryFloorRequest,
    index: _Index,
) -> tuple[ItemState, str] | None:
    rows = _task_rows(task, scope, request, index)
    terminal = max(
        (row for row in rows if row.kind == "TERMINAL"),
        key=_row_key,
        default=None,
    )
    unclaimed = terminal is not None and not any(
        row.kind == "CLAIM" and row.ts > terminal.ts for row in rows
    )
    merged = {pr.key for pr in request.watcher.prs if pr.state == "MERGED"}
    merged.update(merge.key for merge in request.watcher.merges)
    linear = {issue.id: issue.status_type for issue in request.linear}
    if prs and all(pr in merged for pr in prs):
        return "done", "PRs merged"
    if terminal and unclaimed and terminal.outcome in request.config.done_outcomes:
        return "done", f"terminal {terminal.outcome}"
    if task.tickets and all(linear.get(t) == "completed" for t in task.tickets):
        return "done", "Linear done"
    matching = tuple(h for h in holds if _hold_matches(h, task, prs, index))
    for hold in matching:
        if _andon(hold, request.config):
            return "stop", f"andon hold {hold.row_id}: {hold.reason}"
    claim = _live_claim(rows, request, index)
    if claim:
        return "active", f"claim {claim.lane}"
    open_prs = {pr.key for pr in request.watcher.prs if pr.state == "OPEN"}
    for pr in prs:
        if pr in open_prs:
            return "active", f"PR {pr} open"
    if any(linear.get(t) == "started" for t in task.tickets):
        return "active", "Linear in progress"
    if terminal and unclaimed and terminal.outcome in request.config.blocked_outcomes:
        return "wait", f"lane {terminal.lane} ended {terminal.outcome}"
    if matching:
        hold = matching[0]
        return "wait", f"held {hold.row_id}: {hold.reason}"
    return None


def _gate(
    task: ModelFloorManifestTask, request: ModelFactoryFloorRequest
) -> tuple[ItemState, str] | None:
    row = max(
        (
            row
            for row in request.ledger
            if row.kind == "STATUS"
            and row.state == request.config.gate_state
            and row.subject == task.wave
        ),
        key=_row_key,
        default=None,
    )
    if row and row.verdict in request.config.gate_fail_verdicts:
        return "stop", f"gate failed {_stamp(row.ts)}"
    if row and row.verdict in request.config.gate_pass_verdicts:
        return "done", f"gate passed {_stamp(row.ts)}"
    return None


def _manifest_items(
    tasks: tuple[ModelFloorManifestTask, ...],
    plan: ModelFloorPlanInput,
    holds: tuple[ModelFloorLedgerRow, ...],
    request: ModelFactoryFloorRequest,
    index: _Index,
) -> tuple[ModelFloorItem, ...]:
    by_id = {task.id: task for task in tasks}
    prs = {task.id: _task_prs(task, request) for task in tasks}
    memo: dict[str, ModelFloorItem] = {}

    def resolve(task_id: str, visiting: frozenset[str]) -> ModelFloorItem | None:
        if task_id in visiting or task_id not in by_id:
            return None
        if task_id in memo:
            return memo[task_id]
        task = by_id[task_id]
        gate = task.kind == request.config.gate_kind
        evidence = (
            _gate(task, request)
            if gate
            else _evidence(task, prs[task_id], _scope(plan), holds, request, index)
        )
        if evidence is None:
            unmet = tuple(
                dep
                for dep in task.depends_on
                if (item := resolve(dep, visiting | {task_id})) is None
                or item.state != "done"
            )
            state, note = (
                ("wait", f"waits for {','.join(unmet)}")
                if unmet
                else ("prep", "ready to run" if gate else "ready")
            )
        else:
            state, note = evidence
        item = ModelFloorItem(
            id=task.id,
            title=task.title or task.id,
            kind=task.kind,
            state=state,
            note=note[: request.config.note_chars],
            tickets=task.tickets,
            prs=prs[task_id],
        )
        memo[task_id] = item
        return item

    return tuple(
        item for task in tasks if (item := resolve(task.id, frozenset())) is not None
    )


def _table_item(
    row: ModelFloorTableTask,
    holds: tuple[ModelFloorLedgerRow, ...],
    request: ModelFactoryFloorRequest,
) -> ModelFloorItem:
    state: ItemState
    if row.state == "merged":
        state, note = "done", "PRs merged"
    elif row.state in {"open-green", "open-red"}:
        state, note = "active", f"PR open, {row.state.removeprefix('open-')}"
    elif row.state == "not-started":
        state, note = "prep", "not started"
    else:
        state, note = "wait", "state not read"
    if row.state_basis:
        note = f"{note}: {row.state_basis}"
    for hold in holds:
        if _andon(hold, request.config) and _intersects(hold.tickets, row.tickets):
            state, note = "stop", f"andon hold {hold.row_id}: {hold.reason}"
            break
    return ModelFloorItem(
        id=row.label,
        title=row.text or row.label,
        kind="lane",
        state=state,
        note=note[: request.config.note_chars],
        tickets=row.tickets,
        prs=row.prs,
    )


def _station_state(
    items: tuple[ModelFloorItem, ...],
    feeders: tuple[str, ...],
    states: dict[str, ItemState],
) -> ItemState:
    if any(item.state == "stop" for item in items):
        return "stop"
    if all(item.state == "done" for item in items):
        return "done"
    if any(item.state in {"active", "done"} for item in items):
        return "active"
    return "prep" if all(states.get(f) == "done" for f in feeders) else "wait"


def _stations(
    tasks: tuple[ModelFloorManifestTask, ...],
    items: tuple[ModelFloorItem, ...],
    config: ModelFactoryFloorConfig,
) -> tuple[ModelFloorStation, ...]:
    waves = tuple(dict.fromkeys(task.wave for task in tasks)) or ("",)
    station_ids = tuple(wave or config.fallback_station_id for wave in waves)
    task_station = {task.id: task.wave or config.fallback_station_id for task in tasks}
    states: dict[str, ItemState] = {}
    stations: tuple[ModelFloorStation, ...] = ()
    for wave, station_id in zip(waves, station_ids, strict=True):
        members = tuple(
            item for task, item in zip(tasks, items, strict=True) if task.wave == wave
        )
        needed = {
            task_station[dep]
            for task in tasks
            if task.wave == wave
            for dep in task.depends_on
            if dep in task_station and task_station[dep] != station_id
        }
        feeders = tuple(dict.fromkeys(s for s in station_ids if s in needed))
        state = _station_state(members, feeders, states)
        states[station_id] = state
        stations += (
            ModelFloorStation(
                id=station_id,
                name=config.station_name_template.format(wave=wave)
                if wave
                else config.fallback_station_name,
                state=state,
                items=members,
                feeders=feeders,
            ),
        )
    return stations


def _line_id(plan: ModelFloorPlanInput) -> str:
    return plan.plan_path.rsplit("/", 1)[-1].removesuffix(".md")


def _exempts(
    hold: ModelFloorLedgerRow,
    plan: ModelFloorPlanInput,
    request: ModelFactoryFloorRequest,
) -> bool:
    """True when a sentence of the hold's own text exempts this plan's line by name."""
    config = request.config
    slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", _line_id(plan))
    phrase = "-".join(slug.split("-")[: config.hold_exemption_slug_tokens]).casefold()
    markers = tuple(m.casefold() for m in config.hold_exemption_markers)
    return any(
        phrase in sentence and any(m in sentence for m in markers)
        for sentence in (hold.text or hold.reason).casefold().split(". ")
    )


def _red_prs(
    items: tuple[ModelFloorItem, ...], request: ModelFactoryFloorRequest
) -> tuple[str, ...]:
    """The open pull requests of parts being built whose CI is red: the cause a line is really stopped by."""
    red = {
        pr.key: pr.ci_note
        for pr in request.watcher.prs
        if pr.state == "OPEN" and (pr.red or pr.ci == "red")
    }
    found = dict.fromkeys(
        pr for item in items if item.state == "active" for pr in item.prs if pr in red
    )
    return tuple(
        f"PR {pr} CI red" + (f" ({red[pr]})" if red[pr] else "") for pr in found
    )


def _minutes(request: ModelFactoryFloorRequest, ts: datetime) -> int:
    return int((request.generated_at - ts).total_seconds() // 60)


def _resumed(row: ModelFloorLedgerRow, index: _Index) -> bool:
    """True when a lane other than the row's writer claimed one of its tickets after it: the work moved on."""
    for ticket in row.tickets:
        tip = index.claim_tips.get(ticket)
        if tip is None:
            continue
        ts, lane, other = tip
        newest_by_another = ts if lane != row.lane else other
        if newest_by_another is not None and newest_by_another > row.ts:
            return True
    return False


def _asks_orchestrator(
    row: ModelFloorLedgerRow, config: ModelFactoryFloorConfig
) -> bool:
    return any(
        part.strip().startswith(recipient)
        for part in row.to.split(",")
        for recipient in config.ask_recipients
    )


def _ledger_stops(
    plan: ModelFloorPlanInput, request: ModelFactoryFloorRequest, index: _Index
) -> tuple[str, ...]:
    """Open stops the ledger records on the plan's tickets: a lane that ended blocked, an ask nobody answered."""
    config = request.config
    scope = _scope(plan)
    answered = {
        row.re
        for row in request.ledger
        if row.kind in config.ask_answer_kinds and row.re
    }
    stops: tuple[str, ...] = ()
    for row in sorted(request.ledger, key=_row_key):
        if not _intersects(row.tickets, scope) or _resumed(row, index):
            continue
        said = (row.text or row.reason)[: config.note_chars]
        age = _minutes(request, row.ts)
        if row.kind == "TERMINAL" and row.outcome in config.stop_outcomes:
            stops += (f"lane {row.lane} ended {row.outcome} {age} min ago: {said}",)
        elif (
            row.kind == "MSG"
            and _asks_orchestrator(row, config)
            and row.row_id not in answered
        ):
            stops += (f"MSG {row.row_id} to {row.to} unanswered for {age} min: {said}",)
    return stops


def _undrawn(request: ModelFactoryFloorRequest) -> tuple[ModelFloorLine, ...]:
    """A declared line no plan matched is drawn stopped with its cause, never left out of the record."""
    cause = request.config.undrawn_not_found_cause
    return tuple(
        ModelFloorLine(
            line_id=token,
            name=token,
            plan_path=token,
            source="undrawn",
            status_pill="stopped",
            percent_built=0,
            stations=(),
            andon=ModelFloorAndon(
                flag=True,
                reason=f"{cause}: no plan path contains {token}"[
                    : request.config.note_chars
                ],
                causes=(f"{cause}: no plan path contains {token}",),
            ),
        )
        for token in dict.fromkeys(request.selection.plan_path_contains)
        if not any(token in plan.plan_path for plan in request.plans)
    )


def _line(
    plan: ModelFloorPlanInput,
    all_holds: tuple[ModelFloorLedgerRow, ...],
    request: ModelFactoryFloorRequest,
    index: _Index,
) -> ModelFloorLine:
    holds = tuple(h for h in all_holds if not _exempts(h, plan, request))
    manifest = plan.manifest
    has_manifest = manifest is not None and bool(manifest.tasks)
    tasks = (
        manifest.tasks
        if manifest is not None and has_manifest
        else tuple(
            ModelFloorManifestTask(
                id=row.label, title=row.text, tickets=row.tickets, prs=row.prs
            )
            for row in plan.table_tasks
        )
    )
    items = (
        _manifest_items(tasks, plan, holds, request, index)
        if has_manifest
        else tuple(_table_item(row, holds, request) for row in plan.table_tasks)
    )
    causes = (
        tuple(
            f"HOLD {hold.row_id}: {hold.reason or hold.text}"
            for hold in holds
            if _andon(hold, request.config)
            and (
                _intersects(hold.tickets, _scope(plan))
                or any(
                    _hold_matches(hold, task, item.prs, index)
                    for task, item in zip(tasks, items, strict=True)
                )
            )
        )
        + tuple(
            f"GATE {item.id} failed"
            for item in items
            if has_manifest
            and item.kind == request.config.gate_kind
            and item.state == "stop"
        )
        + _red_prs(items, request)
        + _ledger_stops(plan, request, index)
    )
    if not tasks:
        causes = (
            f"{request.config.undrawn_no_tasks_cause}: the plan has neither a task manifest nor a plans-today table",
            *causes,
        )
    reason = (
        causes[0] + (f" (+{len(causes) - 1} more)" if len(causes) > 1 else "")
        if causes
        else ""
    )
    non_gates = tuple(
        item
        for item in items
        if not has_manifest or item.kind != request.config.gate_kind
    )
    percent = (
        100 * sum(item.state == "done" for item in non_gates) // len(non_gates)
        if non_gates
        else 0
    )
    line_id = _line_id(plan)
    return ModelFloorLine(
        line_id=line_id,
        name=(manifest.title if manifest is not None else "") or plan.title or line_id,
        plan_path=plan.plan_path,
        source="manifest" if has_manifest else "plan-task-table",
        status_pill="stopped"
        if causes
        else "done"
        if percent == 100 and items
        else "building"
        if any(i.state in {"done", "active"} for i in items)
        else "queued",
        percent_built=percent,
        stations=_stations(tasks, items, request.config),
        andon=ModelFloorAndon(
            flag=bool(causes), reason=reason[: request.config.note_chars], causes=causes
        ),
    )


def _owner(
    key: str,
    tickets: tuple[str, ...],
    plans: tuple[ModelFloorPlanInput, ...],
    lines: tuple[ModelFloorLine, ...],
    *,
    include_plan_prs: bool,
) -> str | None:
    for plan, line in zip(plans, lines, strict=True):
        if (
            _intersects(tickets, _scope(plan))
            or (include_plan_prs and key in plan.prs)
            or any(
                key in item.prs for station in line.stations for item in station.items
            )
        ):
            return line.line_id
    return None


def _floor(
    request: ModelFactoryFloorRequest,
    plans: tuple[ModelFloorPlanInput, ...],
    lines: tuple[ModelFloorLine, ...],
) -> ModelFloorSection:
    bays: tuple[ModelFloorBay, ...] = ()
    for host in sorted(request.placement, key=lambda host: host.host_name):
        stale = (
            (request.generated_at - host.advertised_at).total_seconds()
            > host.cadence_seconds * request.config.advertisement_stale_factor
        )
        bays += (
            ModelFloorBay(
                host=host.host_name,
                slots=host.max_units,
                running=host.running_units,
                stale=stale,
                free=0 if stale else max(0, host.max_units - host.running_units),
            ),
        )
    since = request.generated_at - timedelta(hours=request.config.shipped_window_hours)
    merges = sorted(
        (
            m
            for m in request.watcher.merges
            if since <= m.merged_at <= request.generated_at
        ),
        key=lambda m: (-m.merged_at.timestamp(), m.key),
    )
    shipped = tuple(
        ModelFloorPrRef(
            pr=m.key,
            title=m.title,
            line_id=_owner(m.key, m.tickets, plans, lines, include_plan_prs=False)
            or "",
            detail=f"merged {_stamp(m.merged_at)}",
        )
        for m in merges[: request.config.shipped_limit]
    )
    waiting = tuple(
        sorted(
            (
                ModelFloorPrRef(
                    pr=pr.key,
                    title=pr.title,
                    line_id=owner,
                    detail="draft"
                    if pr.draft
                    else "ci red"
                    if pr.red
                    else "waiting to land",
                )
                for pr in request.watcher.prs
                if pr.state == "OPEN"
                and (
                    owner := _owner(
                        pr.key, pr.tickets, plans, lines, include_plan_prs=True
                    )
                )
                is not None
            ),
            key=lambda ref: (ref.line_id, ref.pr),
        )
    )[: request.config.waiting_limit]
    stoppages = tuple(
        ModelFloorStoppageOut.model_validate(s, from_attributes=True)
        for s in sorted(
            request.stoppages, key=lambda s: (-s.cost_minutes, -s.occurrences, s.name)
        )[: request.config.stoppage_limit]
    )
    lanes = build_lanes(request, lines)
    return ModelFloorSection(
        machines=build_machines(request, lanes),
        cloud=build_cloud(request),
        lanes=lanes,
        stall_minutes=request.config.stall_minutes,
        lane_window_minutes=request.config.lane_live_window_minutes,
        bays=bays,
        shipped=shipped,
        waiting=waiting,
        stoppages=stoppages,
    )


def compute_factory_floor(request: ModelFactoryFloorRequest) -> ModelFactoryFloorResult:
    """Compute the floor without reading or mutating anything outside the request."""
    selected = tuple(
        plan
        for plan in request.plans
        if not request.selection.plan_path_contains
        or any(text in plan.plan_path for text in request.selection.plan_path_contains)
    )
    holds = _holds(request)
    index = _index(request, _task_ids(selected))
    lines = tuple(_line(plan, holds, request, index) for plan in selected)
    newest = max((row.ts for row in request.ledger), default=None)
    return ModelFactoryFloorResult(
        schema_id=request.config.schema_id,
        generated_at=_stamp(request.generated_at),
        lines=with_application_containers(request, lines + _undrawn(request)),
        floor=_floor(request, selected, lines),
        sources=ModelFloorSources(
            watcher_last_tick=_stamp(request.watcher.last_tick),
            ledger_rows=len(request.ledger),
            ledger_newest_row=_stamp(newest) if newest is not None else "",
            linear_issues=len(request.linear),
            placement_hosts=len(request.placement),
            stoppage_report=bool(request.stoppages),
            pool_read_at=_stamp(max(r.read_at for r in request.pool))
            if request.pool
            else "",
            host_table_read=request.host_table is not None,
            placement_markers_read=request.placed is not None,
            receipts_read=request.receipts is not None,
            cloud_accounts_read=request.cloud_accounts is not None,
            runner_fleet_read=request.runner_fleet is not None,
            runner_fleet_cause=request.runner_fleet_cause,
        ),
    )


class HandlerFactoryFloor:
    """Synchronous adapter for the factory floor compute."""

    def handle(self, request: ModelFactoryFloorRequest) -> ModelFactoryFloorResult:
        """Compute the result from the supplied immutable facts."""
        return compute_factory_floor(request)

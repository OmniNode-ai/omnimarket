# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive a deterministic factory floor from caller-supplied snapshots."""

from __future__ import annotations

import re
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
    request: ModelFactoryFloorRequest,
) -> bool:
    released_prs = {
        pr
        for row in request.ledger
        if row.kind == "RELEASE" and row.re == hold.row_id
        for pr in row.prs
    }
    return (
        hold.task == task.id
        or _token(task.id, hold.reason)
        or _intersects(hold.tickets, task.tickets)
        or any(pr in hold.prs and pr not in released_prs for pr in prs)
    )


def _task_prs(
    task: ModelFloorManifestTask, request: ModelFactoryFloorRequest
) -> tuple[str, ...]:
    inferred = tuple(
        pr.key
        for pr in request.watcher.prs
        if _token(task.id, pr.title)
        or _token(task.id, pr.head_ref)
        or _intersects(task.tickets, pr.tickets)
    )
    return tuple(sorted(set(task.prs + inferred)))


def _task_rows(
    task: ModelFloorManifestTask,
    scope: tuple[str, ...],
    request: ModelFactoryFloorRequest,
) -> tuple[ModelFloorLedgerRow, ...]:
    return tuple(
        row
        for row in request.ledger
        if row.task == task.id
        or _token(task.id, row.reason)
        or _intersects(row.tickets, task.tickets)
        or (_token(task.id, row.text) and _intersects(row.tickets, scope))
    )


def _live_claim(
    rows: tuple[ModelFloorLedgerRow, ...], request: ModelFactoryFloorRequest
) -> ModelFloorLedgerRow | None:
    def live(row: ModelFloorLedgerRow) -> bool:
        lane_rows = tuple(r for r in request.ledger if r.lane == row.lane)
        closed = any(r.kind == "TERMINAL" and r.ts >= row.ts for r in lane_rows)
        newest = max(r.ts for r in lane_rows)
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
) -> tuple[ItemState, str] | None:
    rows = _task_rows(task, scope, request)
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
        return "done", "changes merged"
    if terminal and unclaimed and terminal.outcome in request.config.done_outcomes:
        return "done", f"terminal {terminal.outcome}"
    if task.tickets and all(linear.get(t) == "completed" for t in task.tickets):
        return "done", "Linear done"
    matching = tuple(h for h in holds if _hold_matches(h, task, prs, request))
    for hold in matching:
        if _andon(hold, request.config):
            return "stop", f"stopped by hold {hold.row_id}: {hold.reason}"
    claim = _live_claim(rows, request)
    if claim:
        return "active", f"worker {claim.lane} is on it"
    open_prs = {pr.key for pr in request.watcher.prs if pr.state == "OPEN"}
    for pr in prs:
        if pr in open_prs:
            return "active", f"PR {pr} open"
    if any(linear.get(t) == "started" for t in task.tickets):
        return "active", "Linear in progress"
    if terminal and unclaimed and terminal.outcome in request.config.blocked_outcomes:
        return "wait", f"worker {terminal.lane} ended {terminal.outcome}"
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
            else _evidence(task, prs[task_id], _scope(plan), holds, request)
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
            state, note = "stop", f"stopped by hold {hold.row_id}: {hold.reason}"
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


def _line(
    plan: ModelFloorPlanInput,
    holds: tuple[ModelFloorLedgerRow, ...],
    request: ModelFactoryFloorRequest,
) -> ModelFloorLine:
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
        _manifest_items(tasks, plan, holds, request)
        if has_manifest
        else tuple(_table_item(row, holds, request) for row in plan.table_tasks)
    )
    causes = tuple(
        f"HOLD {hold.row_id}: {hold.reason}"
        for hold in holds
        if _andon(hold, request.config)
        and (
            _intersects(hold.tickets, _scope(plan))
            or any(
                _hold_matches(hold, task, item.prs, request)
                for task, item in zip(tasks, items, strict=True)
            )
        )
    ) + tuple(
        f"GATE {item.id} failed"
        for item in items
        if has_manifest
        and item.kind == request.config.gate_kind
        and item.state == "stop"
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
    line_id = plan.plan_path.rsplit("/", 1)[-1].removesuffix(".md")
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
    return ModelFloorSection(
        bays=bays, shipped=shipped, waiting=waiting, stoppages=stoppages
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
    lines = tuple(_line(plan, holds, request) for plan in selected)
    newest = max((row.ts for row in request.ledger), default=None)
    return ModelFactoryFloorResult(
        schema_id=request.config.schema_id,
        generated_at=_stamp(request.generated_at),
        lines=lines,
        floor=_floor(request, selected, lines),
        sources=ModelFloorSources(
            watcher_last_tick=_stamp(request.watcher.last_tick),
            ledger_rows=len(request.ledger),
            ledger_newest_row=_stamp(newest) if newest is not None else "",
            linear_issues=len(request.linear),
            placement_hosts=len(request.placement),
            stoppage_report=bool(request.stoppages),
        ),
    )


class HandlerFactoryFloor:
    """Synchronous adapter for the factory floor compute."""

    def handle(self, request: ModelFactoryFloorRequest) -> ModelFactoryFloorResult:
        """Compute the result from the supplied immutable facts."""
        return compute_factory_floor(request)

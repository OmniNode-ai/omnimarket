# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Derive a deterministic factory floor from caller-supplied snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from omnimarket.models.model_factory_floor_input import (
    ItemState,
    ModelFactoryFloorConfig,
    ModelFactoryFloorRequest,
    ModelFloorLedgerRow,
    ModelFloorManifestTask,
    ModelFloorPlanInput,
    ModelFloorTableTask,
)
from omnimarket.models.model_floor_phoenix_traces import ModelFloorPhoenixTraces
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_containers import (
    with_application_containers,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_lanes import (
    build_bays,
    build_cloud,
    build_lanes,
    build_machines,
    name_tokens,
    names_task,
    pr_key,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_merged_mapping import (
    PrRecord,
    is_built,
    pr_records,
    task_prs,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    Condition,
    EvidenceKind,
    ModelFactoryFloorResult,
    ModelFloorAndon,
    ModelFloorBlocker,
    ModelFloorDataAge,
    ModelFloorGateRef,
    ModelFloorGates,
    ModelFloorItem,
    ModelFloorLane,
    ModelFloorLine,
    ModelFloorLineEvidence,
    ModelFloorProgress,
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
    # Ledger positions by the typed cells that bind a row to a task: task=, ticket= and pr= (a mention in text binds nothing).
    by_task: dict[str, tuple[int, ...]]
    by_ticket: dict[str, tuple[int, ...]]
    by_pr: dict[str, tuple[int, ...]]
    # Per ticket, the newest CLAIM and the newest CLAIM by any other lane: (ts, lane, other-lane ts).
    claim_tips: dict[str, tuple[datetime, str, datetime | None]]
    # Ledger positions by lane, and each lane's name as words: a lane named for a task (``one-rt-t10-...``) builds it.
    by_lane: dict[str, tuple[int, ...]]
    lane_words: dict[str, tuple[str, ...]]
    # Every open or merged pull request, the merge list included, by key: a task row is built from these (OMN-21930).
    records: dict[str, PrRecord]
    # Integration-proof STATUS rows by pull request, ticket and task id.
    proof_by_pr: dict[str, tuple[int, ...]]
    proof_by_ticket: dict[str, tuple[int, ...]]
    proof_by_task: dict[str, tuple[int, ...]]
    # The merged pull requests shown running on dev (``repo#number``); None when no reader read the records.
    running: frozenset[str] | None
    # When each pull request merged, where the watcher's merge record says.
    merged_at: dict[str, datetime]


@dataclass(frozen=True)
class _Cause:
    """One reason a line is stopped: its sentence and the refs (task ids, pull requests, ledger rows) that show it."""

    text: str
    refs: tuple[str, ...] = ()


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


def _task_map(
    plan: ModelFloorPlanInput, request: ModelFactoryFloorRequest
) -> dict[str, tuple[tuple[str, ...], tuple[str, ...], bool]]:
    """The task-to-ticket map STATUS rows for this plan: per task, its mapped tickets, pull requests and merged flag."""
    found: dict[str, tuple[tuple[str, ...], tuple[str, ...], bool]] = {}
    for row in sorted(request.ledger, key=_row_key):
        if (
            row.kind != "STATUS"
            or row.state != request.config.task_map_state
            or not row.task
            or (row.subject and row.subject not in plan.plan_path)
        ):
            continue
        tickets, prs, merged = found.get(row.task, ((), (), False))
        found[row.task] = (
            tuple(dict.fromkeys(tickets + row.tickets)),
            tuple(dict.fromkeys(prs + row.prs)),
            merged or row.outcome in request.config.done_outcomes,
        )
    return found


def _with_task_map(
    plan: ModelFloorPlanInput, request: ModelFactoryFloorRequest
) -> tuple[ModelFloorPlanInput, frozenset[str]]:
    """The plan with each manifest task's mapped ticket and pull request joined to its own, and the tasks mapped merged."""
    manifest = plan.manifest
    mapping = _task_map(plan, request)
    if manifest is None or not mapping:
        return plan, frozenset()
    tasks = tuple(
        task.model_copy(
            update={
                "tickets": tuple(dict.fromkeys(task.tickets + mapping[task.id][0])),
                "prs": tuple(dict.fromkeys(task.prs + mapping[task.id][1])),
            }
        )
        if task.id in mapping
        else task
        for task in manifest.tasks
    )
    ids = {task.id for task in tasks}
    merged = frozenset(
        task_id
        for task_id, (_t, _p, done) in mapping.items()
        if done and task_id in ids
    )
    return (
        plan.model_copy(
            update={"manifest": manifest.model_copy(update={"tasks": tasks})}
        ),
        merged,
    )


def _index(request: ModelFactoryFloorRequest) -> _Index:
    released: dict[str, set[str]] = {}
    newest: dict[str, datetime] = {}
    terminals: dict[str, list[datetime]] = {}
    by_task: dict[str, list[int]] = {}
    by_ticket: dict[str, list[int]] = {}
    by_pr: dict[str, list[int]] = {}
    tips: dict[str, tuple[datetime, str, datetime | None]] = {}
    by_lane: dict[str, list[int]] = {}
    proof_by_pr: dict[str, list[int]] = {}
    proof_by_ticket: dict[str, list[int]] = {}
    proof_by_task: dict[str, list[int]] = {}
    for position, row in enumerate(request.ledger):
        if row.kind == "STATUS" and row.state == request.config.integration_state:
            for pr in row.prs:
                proof_by_pr.setdefault(pr_key(pr), []).append(position)
            for ticket in row.tickets:
                proof_by_ticket.setdefault(ticket, []).append(position)
            if row.task:
                proof_by_task.setdefault(row.task, []).append(position)
        if row.lane:
            by_lane.setdefault(row.lane, []).append(position)
        if row.kind == "CLAIM":
            for ticket in row.tickets:
                tips[ticket] = _tip(tips.get(ticket), row)
        if row.task:
            by_task.setdefault(row.task, []).append(position)
        for ticket in row.tickets:
            by_ticket.setdefault(ticket, []).append(position)
        for pr in row.prs:
            by_pr.setdefault(pr_key(pr), []).append(position)
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
        by_pr={key: tuple(value) for key, value in by_pr.items()},
        claim_tips=tips,
        by_lane={key: tuple(value) for key, value in by_lane.items()},
        lane_words={lane: name_tokens(lane) for lane in by_lane},
        records=pr_records(request),
        proof_by_pr={key: tuple(value) for key, value in proof_by_pr.items()},
        proof_by_ticket={key: tuple(value) for key, value in proof_by_ticket.items()},
        proof_by_task={key: tuple(value) for key, value in proof_by_task.items()},
        running=None
        if request.running_evidence is None
        else frozenset(
            f"{item.repo.rsplit('/', 1)[-1]}#{item.number}"
            for item in request.running_evidence
            if item.number is not None
        ),
        merged_at={
            pr_key(merge.key): merge.merged_at for merge in request.watcher.merges
        },
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


def _task_rows(
    task: ModelFloorManifestTask,
    scope: tuple[str, ...],
    request: ModelFactoryFloorRequest,
    index: _Index,
) -> tuple[ModelFloorLedgerRow, ...]:
    by_ticket = (p for ticket in task.tickets for p in index.by_ticket.get(ticket, ()))
    if task.scoped:
        # Another plan's lane says "t5" too, so a lane named for the task counts only when it carries a ticket of this plan.
        named = (
            position
            for lane, positions in index.by_lane.items()
            if names_task(task.id, index.lane_words[lane])
            for position in positions
            if _intersects(request.ledger[position].tickets, scope)
        )
        return tuple(
            request.ledger[position] for position in sorted({*by_ticket, *named})
        )
    # Evidence is a row whose typed cells bind it to the task; a task id that only appears in its text binds nothing.
    cells = request.config.evidence_cells
    candidates = {
        *(index.by_task.get(task.id, ()) if "task" in cells else ()),
        *(by_ticket if "ticket" in cells else ()),
        *(
            p
            for pr in (task.prs if "pr" in cells else ())
            for p in index.by_pr.get(pr_key(pr), ())
        ),
    }
    return tuple(request.ledger[position] for position in sorted(candidates))


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
    mapped_merged: bool = False,
) -> tuple[ItemState, str, EvidenceKind] | None:
    """The state a work task's evidence gives it, the note, and what kind of evidence backs a ``done``."""
    rows = _task_rows(task, scope, request, index)
    terminal = max(
        (row for row in rows if row.kind == "TERMINAL"),
        key=_row_key,
        default=None,
    )
    unclaimed = terminal is not None and not any(
        row.kind == "CLAIM" and row.ts > terminal.ts for row in rows
    )
    merged = {
        record.key for record in index.records.values() if record.state == "MERGED"
    }
    linear = {issue.id: issue.status_type for issue in request.linear}
    # Merged work outranks any lane TERMINAL: a stale blocked, partial or never-sent row cannot unbuild it.
    if is_built(task, prs, index.records):
        return "done", "PRs merged", "merged"
    if mapped_merged:
        return "done", "PRs merged (task-to-ticket map)", "merged"
    if terminal and unclaimed and terminal.outcome in request.config.done_outcomes:
        return "done", f"terminal {terminal.outcome}", "terminal"
    if terminal and unclaimed and terminal.outcome in request.config.handoff_outcomes:
        merged_keys = {pr_key(pr) for pr in merged}
        unmerged = tuple(pr for pr in terminal.prs if pr_key(pr) not in merged_keys)
        if terminal.prs and not unmerged:
            return "done", "PRs merged", "merged"
        waiting = ",".join(unmerged) if unmerged else "no pull request named"
        return "active", f"handed to landing by {terminal.lane}: {waiting}", ""
    if task.tickets and all(linear.get(t) == "completed" for t in task.tickets):
        return "done", "Linear done", "linear_done"
    matching = tuple(h for h in holds if _hold_matches(h, task, prs, index))
    for hold in matching:
        if _andon(hold, request.config):
            return "stop", f"andon hold {hold.row_id}: {hold.reason}", ""
    claim = _live_claim(rows, request, index)
    if claim:
        return "active", f"claim {claim.lane}", ""
    open_prs = {pr.key for pr in request.watcher.prs if pr.state == "OPEN"}
    for pr in prs:
        if pr in open_prs:
            return "active", f"PR {pr} open", ""
    if any(linear.get(t) == "started" for t in task.tickets):
        return "active", "Linear in progress", ""
    if terminal and unclaimed and terminal.outcome in request.config.blocked_outcomes:
        return "wait", f"lane {terminal.lane} ended {terminal.outcome}", ""
    if matching:
        hold = matching[0]
        return "wait", f"held {hold.row_id}: {hold.reason}", ""
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
            and row.subject
            and row.subject in {task.wave, task.id}
        ),
        key=_row_key,
        default=None,
    )
    if row and row.verdict in request.config.gate_fail_verdicts:
        return "stop", f"gate failed {_stamp(row.ts)}"
    if row and row.verdict in request.config.gate_pass_verdicts:
        return "done", f"gate passed {_stamp(row.ts)}"
    return None


def _recorded(state: ItemState) -> tuple[ItemState, str, EvidenceKind] | None:
    """The state a manifest records for a task with no other evidence; a recorded done needs a merged pull request."""
    if state == "done":
        return "wait", "recorded done, no merged pull request read", "recorded"
    if state == "prep":
        return None
    return state, f"recorded {state}", "recorded"


def _manifest_items(
    tasks: tuple[ModelFloorManifestTask, ...],
    plan: ModelFloorPlanInput,
    holds: tuple[ModelFloorLedgerRow, ...],
    request: ModelFactoryFloorRequest,
    index: _Index,
    mapped_merged: frozenset[str] = frozenset(),
) -> tuple[ModelFloorItem, ...]:
    by_id = {task.id: task for task in tasks}
    prs = {task.id: task_prs(task, _scope(plan), index.records) for task in tasks}
    memo: dict[str, ModelFloorItem] = {}

    def resolve(task_id: str, visiting: frozenset[str]) -> ModelFloorItem | None:
        if task_id in visiting or task_id not in by_id:
            return None
        if task_id in memo:
            return memo[task_id]
        task = by_id[task_id]
        gate = task.kind == request.config.gate_kind
        evidence: tuple[ItemState, str, EvidenceKind] | None
        if gate:
            verdict = _gate(task, request)
            evidence = None if verdict is None else (verdict[0], verdict[1], "")
        else:
            evidence = _evidence(
                task,
                prs[task_id],
                _scope(plan),
                holds,
                request,
                index,
                task_id in mapped_merged,
            )
        if evidence is None and task.state is not None:
            evidence = _recorded(task.state)
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
            kind: EvidenceKind = ""
        else:
            state, note, kind = evidence
        failed = tuple(
            dep
            for dep in task.depends_on
            if dep in by_id
            and by_id[dep].kind == request.config.gate_kind
            and (verdict := _gate(by_id[dep], request)) is not None
            and verdict[0] == "stop"
        )
        if state == "done" and not gate and failed:
            # Built before its gate passed: shown, but not counted built while the gate reads fail.
            label = request.config.built_ahead_of_gate_label
            state, note = "wait", f"{label} {','.join(failed)}: {note}"
        item = ModelFloorItem(
            id=task.id,
            title=task.title or task.id,
            kind=task.kind,
            state=state,
            note=note[: request.config.note_chars],
            tickets=task.tickets,
            prs=prs[task_id],
            evidence_kind=kind,
        )
        memo[task_id] = item
        return item

    return tuple(
        item for task in tasks if (item := resolve(task.id, frozenset())) is not None
    )


def _table_built(
    row: ModelFloorTableTask, request: ModelFactoryFloorRequest, index: _Index
) -> EvidenceKind:
    """What builds a table row: merged pull requests, or its tickets done in Linear; never its recorded state ("" if none)."""
    task = ModelFloorManifestTask(
        id=row.label, tickets=row.tickets, prs=row.prs, scoped=True
    )
    if is_built(task, task_prs(task, row.tickets, index.records), index.records):
        return "merged"
    linear = {issue.id: issue.status_type for issue in request.linear}
    if row.tickets and all(linear.get(t) == "completed" for t in row.tickets):
        return "linear_done"
    return ""


def _table_item(
    row: ModelFloorTableTask,
    holds: tuple[ModelFloorLedgerRow, ...],
    request: ModelFactoryFloorRequest,
    index: _Index,
) -> ModelFloorItem:
    state: ItemState
    kind: EvidenceKind = ""
    if row.state == "merged":
        kind = _table_built(row, request, index)
        state, note = (
            ("done", "Linear done" if kind == "linear_done" else "PRs merged")
            if kind
            else ("wait", "recorded merged, no merged pull request read")
        )
        kind = kind or "recorded"
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
        evidence_kind=kind,
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


def _task_stations(
    tasks: tuple[ModelFloorManifestTask, ...],
    items: tuple[ModelFloorItem, ...],
    config: ModelFactoryFloorConfig,
) -> tuple[ModelFloorStation, ...]:
    """Every task its own station: the task's state and tickets, fed by the tasks it depends on."""
    ids = {task.id for task in tasks}
    return tuple(
        ModelFloorStation(
            id=task.id,
            name=item.title,
            state=item.state,
            items=(item,),
            feeders=tuple(dict.fromkeys(d for d in task.depends_on if d in ids)),
            group=config.station_name_template.format(wave=task.wave)
            if task.wave
            else "",
            tickets=item.tickets,
            prs=item.prs,
        )
        for task, item in zip(tasks, items, strict=True)
    )


def _stations(
    tasks: tuple[ModelFloorManifestTask, ...],
    items: tuple[ModelFloorItem, ...],
    config: ModelFactoryFloorConfig,
) -> tuple[ModelFloorStation, ...]:
    if config.station_grouping == "task":
        return _task_stations(tasks, items, config)
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
                group=config.station_name_template.format(wave=wave) if wave else "",
                tickets=tuple(
                    dict.fromkeys(t for item in members for t in item.tickets)
                ),
                prs=tuple(dict.fromkeys(p for item in members for p in item.prs)),
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
) -> tuple[_Cause, ...]:
    """The open pull requests of parts being built whose CI is red: the cause a line is really stopped by."""
    red = {
        pr.key: pr.ci_note
        for pr in request.watcher.prs
        if pr.state == "OPEN" and (pr.red or pr.ci == "red")
    }
    found = dict.fromkeys(
        (pr, item.id)
        for item in items
        if item.state == "active"
        for pr in item.prs
        if pr in red
    )
    return tuple(
        _Cause(f"PR {pr} CI red" + (f" ({red[pr]})" if red[pr] else ""), (pr, task))
        for pr, task in found
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


def _ended_long_ago(
    row: ModelFloorLedgerRow, request: ModelFactoryFloorRequest, index: _Index
) -> bool:
    """True when the row is a TERMINAL its lane never reopened and the lane has written nothing for the stall window.

    The runner that closes a remote lane writes a STATUS row after the lane's own TERMINAL, so the lane's newest row of
    any kind, not the TERMINAL itself, is what has to be older than the window.
    """
    if row.kind != "TERMINAL":
        return False
    lane_rows = (
        request.ledger[position] for position in index.by_lane.get(row.lane, ())
    )
    if any(other.kind == "CLAIM" and other.ts > row.ts for other in lane_rows):
        return False
    newest = index.lane_newest.get(row.lane, row.ts)
    return _minutes(request, newest) >= request.config.stall_minutes


def _asks_orchestrator(
    row: ModelFloorLedgerRow, config: ModelFactoryFloorConfig
) -> bool:
    return any(
        part.strip().startswith(recipient)
        for part in row.to.split(",")
        for recipient in config.ask_recipients
    )


def _ledger_refs(row: ModelFloorLedgerRow) -> tuple[str, ...]:
    """The refs that show a ledger stop: the row's id (else its lane) and the pull requests it names."""
    return tuple(dict.fromkeys((row.row_id or row.lane, *row.prs)))


def _ledger_stops(
    plan: ModelFloorPlanInput,
    request: ModelFactoryFloorRequest,
    index: _Index,
    items: tuple[ModelFloorItem, ...],
) -> tuple[_Cause, ...]:
    """Open stops the ledger records on the plan's tickets: a lane that ended blocked, an ask nobody answered.

    A stop is over when the work moved on (another lane claimed its ticket after it), when every pull request it names
    has merged, or when every ticket it names belongs only to parts of this plan that are done.
    """
    config = request.config
    scope = _scope(plan)
    answered = {
        row.re
        for row in request.ledger
        if row.kind in config.ask_answer_kinds and row.re
    }
    merged = {pr_key(pr.key) for pr in request.watcher.prs if pr.state == "MERGED"}
    merged.update(pr_key(merge.key) for merge in request.watcher.merges)
    done_tickets = {
        ticket for item in items if item.state == "done" for ticket in item.tickets
    }
    stops: tuple[_Cause, ...] = ()
    for row in sorted(request.ledger, key=_row_key):
        if not _intersects(row.tickets, scope) or _resumed(row, index):
            continue
        if row.prs and all(pr_key(pr) in merged for pr in row.prs):
            continue
        if row.tickets and set(row.tickets) <= done_tickets:
            continue
        if _ended_long_ago(row, request, index):
            continue
        said = (row.reason or row.text)[: config.note_chars]
        age = _minutes(request, row.ts)
        if row.kind == "TERMINAL" and row.outcome in config.stop_outcomes:
            stops += (
                _Cause(
                    f"lane {row.lane} ended {row.outcome} {age} min ago: {said}",
                    _ledger_refs(row),
                ),
            )
        elif (
            row.kind == "MSG"
            and _asks_orchestrator(row, config)
            and row.row_id not in answered
        ):
            stops += (
                _Cause(
                    f"MSG {row.row_id} to {row.to} unanswered for {age} min: {said}",
                    _ledger_refs(row),
                ),
            )
    return stops


def _is_gate(item: ModelFloorItem, request: ModelFactoryFloorRequest) -> bool:
    return item.kind == request.config.gate_kind


def _work_items(
    items: tuple[ModelFloorItem, ...], request: ModelFactoryFloorRequest
) -> tuple[ModelFloorItem, ...]:
    """The work tasks: every item whose kind is not the contract's gate kind. Gates are never counted as work."""
    return tuple(item for item in items if not _is_gate(item, request))


def _merged_work(work: tuple[ModelFloorItem, ...]) -> tuple[ModelFloorItem, ...]:
    """The work tasks done by merged evidence (a merged pull request or the task-to-ticket map's merged outcome) only.

    A task done only by a terminal outcome or a closed Linear issue is recorded done: it counts toward the condition,
    never toward the progress.
    """
    return tuple(
        item for item in work if item.state == "done" and item.evidence_kind == "merged"
    )


def _gates(
    items: tuple[ModelFloorItem, ...], request: ModelFactoryFloorRequest
) -> ModelFloorGates:
    """The gates counted apart from the work: passed (done), failed (stop) and waiting (anything else), with their ids."""
    refs = tuple(
        ModelFloorGateRef(
            id=item.id,
            state="passed"
            if item.state == "done"
            else "failed"
            if item.state == "stop"
            else "waiting",
        )
        for item in items
        if _is_gate(item, request)
    )
    return ModelFloorGates(
        passed=sum(ref.state == "passed" for ref in refs),
        failed=sum(ref.state == "failed" for ref in refs),
        waiting=sum(ref.state == "waiting" for ref in refs),
        items=refs,
    )


def _percent(completed: int, total: int, decimals: int) -> float:
    """``completed / total x 100`` rounded half-up to ``decimals`` places (6 of 48 is 12.5, never truncated to 12)."""
    exact = Decimal(completed) * 100 / Decimal(total)
    return float(exact.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP))


def _progress(
    plan: ModelFloorPlanInput,
    items: tuple[ModelFloorItem, ...],
    request: ModelFactoryFloorRequest,
) -> ModelFloorProgress:
    """Merged work tasks over valid work tasks; unknown is never 0.

    ``unknown`` when the plan's scope is under review, its task ids repeat, or no manifest and no table was read;
    ``no_tasks`` when zero work tasks are declared; ``known`` otherwise, a real 0 completed included.
    """
    config = request.config
    work = _work_items(items, request)
    manifest = plan.manifest
    if manifest is not None and manifest.scope_status == "needs_review":
        return ModelFloorProgress(
            state="unknown",
            completed=None,
            total=None,
            percent=None,
            note=config.progress_note_under_review.format(
                note=manifest.scope_note
            ).strip()[: config.note_chars],
        )
    ids = tuple(task.id for task in manifest.tasks) if manifest is not None else ()
    if len(ids) != len(set(ids)):
        return ModelFloorProgress(
            state="unknown",
            completed=None,
            total=None,
            percent=None,
            note=config.progress_note_duplicate_ids,
        )
    if manifest is None and not plan.table_tasks:
        return ModelFloorProgress(
            state="unknown",
            completed=None,
            total=None,
            percent=None,
            note=config.progress_note_unscoped,
        )
    if not work:
        return ModelFloorProgress(
            state="no_tasks",
            completed=None,
            total=0,
            percent=None,
            note=config.progress_note_no_tasks,
        )
    completed = len(_merged_work(work))
    return ModelFloorProgress(
        state="known",
        completed=completed,
        total=len(work),
        percent=_percent(completed, len(work), config.percent_decimals),
        note=config.progress_note_known.format(completed=completed, total=len(work)),
    )


def _proof(
    item: ModelFloorItem,
    prs: tuple[str, ...],
    request: ModelFactoryFloorRequest,
    index: _Index,
) -> ModelFloorLedgerRow | None:
    """The newest integration-proof row naming the item, written after the item's newest known merge."""
    positions = {
        *(p for pr in prs for p in index.proof_by_pr.get(pr_key(pr), ())),
        *(p for ticket in item.tickets for p in index.proof_by_ticket.get(ticket, ())),
        *index.proof_by_task.get(item.id, ()),
    }
    merged = [
        index.merged_at[pr_key(pr)] for pr in prs if pr_key(pr) in index.merged_at
    ]
    since = max(merged, default=None)
    return max(
        (
            row
            for row in (request.ledger[p] for p in positions)
            if since is None or row.ts >= since
        ),
        key=_row_key,
        default=None,
    )


def _line_evidence(
    work: tuple[ModelFloorItem, ...],
    request: ModelFactoryFloorRequest,
    index: _Index,
) -> ModelFloorLineEvidence:
    """Counts over the work tasks, one per kind of evidence, never added together.

    ``merged`` counts merged work tasks. ``running_on_dev`` counts those whose merged pull requests all appear in the
    running-on-dev records, and ``verified`` those that also have a passing integration-proof row written after the
    merge. Both are None when the running-on-dev records were not read: unread is not 0.
    """
    merged = _merged_work(work)
    if index.running is None:
        return ModelFloorLineEvidence(
            merged=len(merged), running_on_dev=None, verified=None, of=len(work)
        )
    merged_keys = {
        pr_key(record.key)
        for record in index.records.values()
        if record.state == "MERGED"
    }
    running = verified = 0
    for item in merged:
        named = tuple(pr for pr in item.prs if pr_key(pr) in merged_keys) or item.prs
        if not named or any(pr_key(pr) not in index.running for pr in named):
            continue
        running += 1
        proof = _proof(item, named, request, index)
        if (
            proof is not None
            and proof.verdict in request.config.integration_pass_verdicts
        ):
            verified += 1
    return ModelFloorLineEvidence(
        merged=len(merged), running_on_dev=running, verified=verified, of=len(work)
    )


def _data_age(request: ModelFactoryFloorRequest) -> ModelFloorDataAge:
    """The oldest read among the sources a line depends on, and its age in minutes against the request's own time.

    The watcher's tick is always one; the ledger, Linear and running-on-dev reads count when the caller supplied
    their read times. A source with no read time is not guessed.
    """
    reads = request.source_reads
    stamps = [request.watcher.last_tick]
    stamps += [
        stamp
        for stamp in (
            reads.ledger,
            reads.linear,
            reads.running_evidence if request.running_evidence is not None else None,
        )
        if stamp is not None
    ]
    oldest = min(stamps)
    return ModelFloorDataAge(
        read_at=_stamp(oldest), age_min=max(0, _minutes(request, oldest))
    )


def _condition(
    *,
    progress: ModelFloorProgress,
    work: tuple[ModelFloorItem, ...],
    items: tuple[ModelFloorItem, ...],
    gates: ModelFloorGates,
    hard: bool,
    causes: bool,
) -> Condition:
    """The one condition of the line.

    stopped for a hard cause (an andon hold, a failed gate, no task table); needs_review while progress is unknown;
    stopped for a soft cause (a red pull request, a ledger stop; ``_running`` turns it building while a live lane works
    the line); complete only when every work task is done by merged, terminal or closed-ticket evidence and every gate
    has passed; building when work is done or active; else queued.
    """
    if hard:
        return "stopped"
    if progress.state == "unknown":
        return "needs_review"
    if causes:
        return "stopped"
    if (
        progress.state == "known"
        and all(item.state == "done" for item in work)
        and gates.passed == len(gates.items)
    ):
        return "complete"
    if any(item.state in {"done", "active"} for item in items):
        return "building"
    return "queued"


def _blocker(
    causes: tuple[_Cause, ...],
    work: tuple[ModelFloorItem, ...],
    gates: ModelFloorGates,
    request: ModelFactoryFloorRequest,
) -> ModelFloorBlocker | None:
    """The first cause as the reason with its refs; else the pending gate of a line whose work is all done; else None."""
    if causes:
        first = causes[0]
        more = f" (+{len(causes) - 1} more)" if len(causes) > 1 else ""
        return ModelFloorBlocker(
            reason=(first.text + more)[: request.config.note_chars],
            evidence=first.refs,
        )
    waiting = tuple(ref.id for ref in gates.items if ref.state == "waiting")
    if work and waiting and all(item.state == "done" for item in work):
        return ModelFloorBlocker(
            reason=request.config.pending_gate_blocker.format(gates=",".join(waiting))[
                : request.config.note_chars
            ],
            evidence=waiting,
        )
    return None


def _undrawn(request: ModelFactoryFloorRequest) -> tuple[ModelFloorLine, ...]:
    """A declared line no plan matched is drawn stopped with its cause, never left out of the record."""
    cause = request.config.undrawn_not_found_cause
    return tuple(
        ModelFloorLine(
            line_id=token,
            name=token,
            plan_path=token,
            source="undrawn",
            condition="stopped",
            blocker=ModelFloorBlocker(
                reason=f"{cause}: no plan path contains {token}"[
                    : request.config.note_chars
                ],
                evidence=(token,),
            ),
            progress=ModelFloorProgress(
                state="unknown",
                completed=None,
                total=None,
                percent=None,
                note=request.config.progress_note_unscoped,
            ),
            gates=ModelFloorGates(passed=0, failed=0, waiting=0, items=()),
            line_evidence=ModelFloorLineEvidence(
                merged=0,
                running_on_dev=None,
                verified=None,
                of=0,
            ),
            data_age=_data_age(request),
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
    mapped_merged: frozenset[str] = frozenset(),
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
        _manifest_items(tasks, plan, holds, request, index, mapped_merged)
        if has_manifest
        else tuple(_table_item(row, holds, request, index) for row in plan.table_tasks)
    )
    hard = (
        (
            _Cause(
                f"{request.config.undrawn_no_tasks_cause}: the plan has neither a task manifest nor a plans-today table"
            ),
        )
        if not tasks
        else ()
    ) + tuple(
        _Cause(
            f"HOLD {hold.row_id}: {hold.reason or hold.text}",
            (hold.row_id,) if hold.row_id else (),
        )
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
    hard += tuple(
        _Cause(f"GATE {item.id} failed", (item.id,))
        for item in items
        if has_manifest and _is_gate(item, request) and item.state == "stop"
    )
    causes = (
        hard + _red_prs(items, request) + _ledger_stops(plan, request, index, items)
    )
    reason = (
        causes[0].text + (f" (+{len(causes) - 1} more)" if len(causes) > 1 else "")
        if causes
        else ""
    )
    work = _work_items(items, request)
    progress = _progress(plan, items, request)
    gates = _gates(items, request)
    line_id = _line_id(plan)
    return ModelFloorLine(
        line_id=line_id,
        name=(manifest.title if manifest is not None else "") or plan.title or line_id,
        plan_path=plan.plan_path,
        source="manifest" if has_manifest else "plan-task-table",
        condition=_condition(
            progress=progress,
            work=work,
            items=items,
            gates=gates,
            hard=bool(hard),
            causes=bool(causes),
        ),
        blocker=_blocker(causes, work, gates, request),
        progress=progress,
        gates=gates,
        line_evidence=_line_evidence(work, request, index),
        data_age=_data_age(request),
        stations=_stations(tasks, items, request.config),
        andon=ModelFloorAndon(
            flag=bool(causes),
            reason=reason[: request.config.note_chars],
            causes=tuple(cause.text for cause in causes),
            hard=bool(hard),
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
    scopes = {_line_id(plan): _scope(plan) for plan in plans}
    lanes = build_lanes(request, lines, scopes)
    machines = build_machines(request, lanes)
    return ModelFloorSection(
        machines=machines,
        cloud=build_cloud(request),
        lanes=lanes,
        stall_minutes=request.config.stall_minutes,
        lane_window_minutes=request.config.lane_live_window_minutes,
        bays=build_bays(machines),
        shipped=shipped,
        waiting=waiting,
        stoppages=stoppages,
        phoenix_traces=request.phoenix_traces
        or ModelFloorPhoenixTraces(
            state="unread", detail="no Phoenix reading was supplied"
        ),
    )


def _running(
    lines: tuple[ModelFloorLine, ...], lanes: tuple[ModelFloorLane, ...]
) -> tuple[ModelFloorLine, ...]:
    """A line stopped only by soft causes (red pull requests, ledger stops) is building while a live lane works on it.

    Only the condition changes: the blocker stays, and no other field reads stopped.
    """
    working = {
        lane.station.line_id
        for lane in lanes
        if lane.station is not None and not lane.stalled
    }
    return tuple(
        line.model_copy(update={"condition": "building"})
        if line.condition == "stopped"
        and line.andon.flag
        and not line.andon.hard
        and line.line_id in working
        else line
        for line in lines
    )


def compute_factory_floor(request: ModelFactoryFloorRequest) -> ModelFactoryFloorResult:
    """Compute the floor without reading or mutating anything outside the request."""
    selected = tuple(
        plan
        for plan in request.plans
        if not request.selection.plan_path_contains
        or any(text in plan.plan_path for text in request.selection.plan_path_contains)
    )
    mapped = tuple(_with_task_map(plan, request) for plan in selected)
    selected = tuple(plan for plan, _merged in mapped)
    holds = _holds(request)
    index = _index(request)
    lines = tuple(_line(plan, holds, request, index, merged) for plan, merged in mapped)
    newest = max((row.ts for row in request.ledger), default=None)
    floor = _floor(request, selected, lines)
    lines = _running(lines, floor.lanes)
    return ModelFactoryFloorResult(
        schema_id=request.config.schema_id,
        generated_at=_stamp(request.generated_at),
        lines=with_application_containers(request, lines + _undrawn(request)),
        floor=floor,
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

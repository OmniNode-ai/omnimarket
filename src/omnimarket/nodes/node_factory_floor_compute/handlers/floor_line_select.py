# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Select the factory floor's lines by six classes, each line once at its lowest class (OMN-22369).

The classes, in order: declared (the overlay's ``plan_path_contains``), plan (selected by plans-today), goal (an
open goal-file row), ruling (a ruling of the window that maps to no line above), ticket (unplanned work: a live
claim, an unresolved lane end, an open or unrun pull request, an In Progress ticket, mapped to no line above) and
process (a factory process the overlay declares). Pure: every fact and the time come from the request, and the
vocabulary from the contract's ``line_select`` block. The compute holds no list of lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorLedgerRow,
    ModelFloorPlanInput,
)
from omnimarket.models.model_floor_line_select import (
    HealthState,
    LineClass,
    ModelFloorClassRule,
    ModelFloorSourceHealth,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_lanes import pr_key
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    LineSource,
    ModelFloorAndon,
    ModelFloorCoverage,
    ModelFloorDataAge,
    ModelFloorGates,
    ModelFloorLine,
    ModelFloorLineEvidence,
    ModelFloorProgress,
)

# The worst health first: a class is as complete as its least complete source.
_WORST: tuple[HealthState, ...] = ("unread", "stale", "read-empty", "read")


@dataclass(frozen=True)
class PlanPick:
    """A plan the selection drew, the class that drew it and its place in that class (the overlay's order)."""

    plan: ModelFloorPlanInput
    line_class: LineClass
    position: int


def line_id_of(plan: ModelFloorPlanInput) -> str:
    """The plan file's base name: the id a declared or plan line has always had."""
    return plan.plan_path.rsplit("/", 1)[-1].removesuffix(".md")


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def class_rank(request: ModelFactoryFloorRequest, line_class: LineClass) -> int:
    return [r.line_class for r in request.config.line_select.classes].index(line_class)


def _rule(
    request: ModelFactoryFloorRequest, line_class: LineClass
) -> ModelFloorClassRule:
    return request.config.line_select.classes[class_rank(request, line_class)]


def health(request: ModelFactoryFloorRequest, source: str) -> ModelFloorSourceHealth:
    """The caller's health entry for a source; a source it did not report on is unread, never read."""
    for entry in request.source_health:
        if entry.source == source:
            return entry
    return ModelFloorSourceHealth(
        source=source,
        state="unread",
        cause=request.config.line_select.missing_health_cause,
    )


def pick_plans(request: ModelFactoryFloorRequest) -> tuple[PlanPick, ...]:
    """The plans that are lines: declared by overlay text, or selected by plans-today, each once at its lowest class.

    A run that declares no line and supplies no plans-today paths draws every plan it is given, as the floor always has.
    """
    selection = request.selection
    tokens = selection.plan_path_contains
    if selection.plans_today_paths is None and not tokens:
        return tuple(
            PlanPick(plan, "declared", position)
            for position, plan in enumerate(request.plans)
        )
    enabled = set(selection.line_classes)
    today = (
        frozenset(selection.plans_today_paths or ())
        if "plan" in enabled and health(request, "plans_today").state != "unread"
        else frozenset()
    )
    picks: list[PlanPick] = []
    for plan in request.plans:
        declared_at = next(
            (n for n, token in enumerate(tokens) if token in plan.plan_path), None
        )
        if declared_at is not None and "declared" in enabled:
            picks.append(PlanPick(plan, "declared", declared_at))
        if plan.plan_path in today:
            picks.append(PlanPick(plan, "plan", 0))
    return lowest_class_once(request, tuple(picks))


def lowest_class_once(
    request: ModelFactoryFloorRequest, picks: tuple[PlanPick, ...]
) -> tuple[PlanPick, ...]:
    """Order by class, then the overlay's order, then id; a plan chosen by two classes is one line, at the lowest."""
    seen: set[str] = set()
    ordered: list[PlanPick] = []
    for pick in sorted(
        picks,
        key=lambda p: (
            class_rank(request, p.line_class),
            p.position,
            line_id_of(p.plan),
        ),
    ):
        if line_id_of(pick.plan) not in seen:
            seen.add(line_id_of(pick.plan))
            ordered.append(pick)
    return tuple(ordered)


def missing_plan_paths(request: ModelFactoryFloorRequest) -> tuple[str, ...]:
    """Plan files plans-today selected that no plan was supplied for: drawn stopped, never left out."""
    selection = request.selection
    if (
        selection.plans_today_paths is None
        or "plan" not in selection.line_classes
        or health(request, "plans_today").state == "unread"
    ):
        return ()
    have = {plan.plan_path for plan in request.plans}
    return tuple(
        dict.fromkeys(path for path in selection.plans_today_paths if path not in have)
    )


def _bare_line(
    request: ModelFactoryFloorRequest,
    *,
    line_id: str,
    name: str,
    source: LineSource,
    line_class: LineClass,
    data_age: ModelFloorDataAge,
    ruled_by: tuple[str, ...] = (),
) -> ModelFloorLine:
    """A line with no plan behind it: stages are not computed for its class yet, so nothing is said to be built."""
    return ModelFloorLine(
        line_id=line_id,
        name=name or line_id,
        plan_path=line_id,
        source=source,
        line_class=line_class,
        ruled_by=ruled_by,
        condition="needs_review",
        blocker=None,
        progress=ModelFloorProgress(
            state="unknown",
            completed=None,
            total=None,
            percent=None,
            note=request.config.line_select.bare_progress_note,
        ),
        gates=ModelFloorGates(passed=0, failed=0, waiting=0, items=()),
        line_evidence=ModelFloorLineEvidence(
            merged=0, running_on_dev=None, verified=None, of=0
        ),
        data_age=data_age,
        stations=(),
        andon=ModelFloorAndon(flag=False, reason="", causes=()),
    )


def _running(request: ModelFactoryFloorRequest) -> frozenset[str] | None:
    if request.running_evidence is None:
        return None
    return frozenset(
        f"{item.repo.rsplit('/', 1)[-1]}#{item.number}"
        for item in request.running_evidence
        if item.number is not None
    )


def _ticket_candidates(
    request: ModelFactoryFloorRequest, live_claims: tuple[ModelFloorLedgerRow, ...]
) -> dict[str, str]:
    """Tickets whose work is in progress and not yet running, each with the evidence that names it (class ticket)."""
    cfg = request.config.line_select
    cutoff = request.generated_at - timedelta(days=cfg.selection_window_days)
    running = _running(request)
    open_prs: set[str] = set()
    unrun: set[str] = set()
    ran: dict[str, bool] = {}
    for pr in request.watcher.prs:
        if pr.state == "OPEN":
            open_prs.update(pr.tickets)
    for merge in request.watcher.merges:
        for ticket in merge.tickets:
            is_running = running is not None and pr_key(merge.key) in running
            ran[ticket] = ran.get(ticket, True) and is_running
            if running is not None and not is_running:
                unrun.add(ticket)
    reached_running = {t for t, all_run in ran.items() if all_run and t not in open_prs}
    unresolved = set(request.config.blocked_outcomes) | set(
        request.config.handoff_outcomes
    )
    found: dict[str, str] = {}

    def add(ticket: str, evidence: str) -> None:
        found.setdefault(ticket, evidence)

    for claim in live_claims:
        for ticket in claim.tickets:
            add(ticket, claim.row_id or f"{cfg.claim_kind} {claim.lane}")
    for row in request.ledger:
        # A lane end older than the window is judged by the resolution rules (plan task T5), not selected here: the
        # effect forwards unresolved ends from the archive rolls with no age limit, and this selection does not read
        # them as work in progress.
        if row.kind != cfg.terminal_kind or row.ts < cutoff:
            continue
        for ticket in row.tickets:
            evidence = row.row_id or f"{cfg.terminal_kind} {row.lane}"
            if row.outcome in unresolved:
                if ticket not in reached_running:
                    add(ticket, evidence)
            elif ticket in open_prs or ticket in unrun:
                add(ticket, evidence)
    for ticket in sorted(open_prs | unrun):
        add(ticket, f"pull request on {ticket}")
    closed = {
        issue.id
        for issue in request.linear
        if issue.status_type in cfg.closed_status_types
    }
    for issue in request.linear:
        if issue.status in cfg.in_progress_statuses:
            add(issue.id, f"{issue.id} {issue.status}")
    return {
        ticket: evidence
        for ticket, evidence in found.items()
        if not (ticket in closed and ticket not in open_prs and ticket not in unrun)
    }


def select_extra_lines(
    request: ModelFactoryFloorRequest,
    plan_lines: tuple[ModelFloorLine, ...],
    plan_scopes: dict[str, tuple[str, ...]],
    live_claims: tuple[ModelFloorLedgerRow, ...],
    data_age: ModelFloorDataAge,
) -> tuple[tuple[ModelFloorLine, ...], dict[str, tuple[str, ...]]]:
    """Classes goal, ruling, ticket and process: the lines, and the rulings that map onto lines of classes 1 to 3 and 5."""
    cfg = request.config.line_select
    enabled = set(request.selection.line_classes)
    lines: list[ModelFloorLine] = []
    ruled: dict[str, list[str]] = {}

    mapped: dict[str, str] = {}  # ticket -> line id, for every line above class 5
    for line in plan_lines:
        for ticket in plan_scopes.get(line.line_id, ()):
            mapped.setdefault(ticket, line.line_id)

    goal_rule = _rule(request, "goal")
    goal_ids: dict[str, str] = {}  # ticket -> goal line id
    if "goal" in enabled:
        for goal in sorted(request.goal_rows or (), key=lambda g: g.row_id):
            if not goal.is_open:
                continue
            line_id = f"{goal_rule.id_prefix}{goal.row_id}"
            lines.append(
                _bare_line(
                    request,
                    line_id=line_id,
                    name=goal.name,
                    source="goal",
                    line_class="goal",
                    data_age=data_age,
                )
            )
            for ticket in goal.tickets:
                mapped.setdefault(ticket, line_id)
                goal_ids.setdefault(ticket, line_id)

    candidates = _ticket_candidates(request, live_claims) if "ticket" in enabled else {}
    ticket_rule = _rule(request, "ticket")
    ruling_rule = _rule(request, "ruling")
    ruling_lines: list[ModelFloorLine] = []
    if "ruling" in enabled:
        cutoff = request.generated_at - timedelta(days=cfg.selection_window_days)
        for row in sorted(
            (r for r in request.ledger if r.kind == cfg.ruling_kind and r.ts >= cutoff),
            key=lambda r: (r.ts, r.row_id),
        ):
            ruling_id = row.row_id or _stamp(row.ts)
            targets = [
                line.line_id
                for line in plan_lines
                if set(row.tickets) & set(plan_scopes.get(line.line_id, ()))
                or (row.subject and row.subject in line.plan_path)
            ]
            targets += [
                mapped[t]
                for t in row.tickets
                if t in mapped and mapped[t] not in targets
            ]
            targets += [
                f"{ticket_rule.id_prefix}{t}"
                for t in row.tickets
                if t in candidates and t not in mapped
            ]
            if targets:
                for target in dict.fromkeys(targets):
                    ruled.setdefault(target, []).append(ruling_id)
                continue
            ruling_lines.append(
                _bare_line(
                    request,
                    line_id=f"{ruling_rule.id_prefix}{ruling_id}",
                    name=row.text or row.reason,
                    source="ruling",
                    line_class="ruling",
                    data_age=data_age,
                )
            )
            for ticket in row.tickets:
                mapped.setdefault(ticket, ruling_lines[-1].line_id)
    lines.extend(ruling_lines)

    titles = {issue.id: issue.title for issue in request.linear}
    for ticket in sorted(candidates):
        if ticket in mapped:
            continue
        line_id = f"{ticket_rule.id_prefix}{ticket}"
        lines.append(
            _bare_line(
                request,
                line_id=line_id,
                name=titles.get(ticket) or ticket,
                source="ticket",
                line_class="ticket",
                data_age=data_age,
                ruled_by=tuple(ruled.get(line_id, ())),
            )
        )

    if "process" in enabled:
        process_rule = _rule(request, "process")
        lines.extend(
            _bare_line(
                request,
                line_id=f"{process_rule.id_prefix}{decl.name}",
                name=decl.title or decl.name,
                source="process",
                line_class="process",
                data_age=data_age,
            )
            for decl in dict.fromkeys(request.processes)
        )

    process_order = {
        f"{_rule(request, 'process').id_prefix}{decl.name}": n
        for n, decl in enumerate(request.processes)
    }
    ranked = sorted(
        lines,
        key=lambda line: (
            class_rank(request, line.line_class),
            process_order.get(line.line_id, 0),
            line.line_id,
        ),
    )
    # One line per id: two rows of one id (a ruling row and its twin) are one line, the first in class order.
    unique: dict[str, ModelFloorLine] = {}
    for line in ranked:
        unique.setdefault(line.line_id, line)
    out = tuple(
        line.model_copy(update={"ruled_by": tuple(ruled[line.line_id])})
        if line.line_id in ruled and not line.ruled_by
        else line
        for line in unique.values()
    )
    return out, {key: tuple(value) for key, value in ruled.items()}


def coverage(
    request: ModelFactoryFloorRequest, lines: tuple[ModelFloorLine, ...]
) -> tuple[ModelFloorCoverage, ...]:
    """Per drawn class, the worst health among its sources, the oldest read, and a count only when it can be trusted."""
    cfg = request.config.line_select
    enabled = set(request.selection.line_classes)
    out: list[ModelFloorCoverage] = []
    for rule in cfg.classes:
        if rule.line_class not in enabled:
            continue
        entries = [health(request, source) for source in rule.sources]
        state: HealthState = (
            min((e.state for e in entries), key=_WORST.index) if entries else "read"
        )
        reads = [e.read_at for e in entries if e.read_at is not None]
        read_at = _stamp(min(reads)) if reads else ""
        count = (
            None
            if state == "unread"
            else sum(1 for line in lines if line.line_class == rule.line_class)
        )
        note = ""
        if state == "stale" and read_at:
            note = cfg.stale_note.format(label=rule.label, read_at=read_at)
        elif state == "unread":
            note = cfg.unread_note.format(label=rule.label)
        out.append(
            ModelFloorCoverage(
                line_class=rule.line_class,
                state=state,
                read_at=read_at,
                lines=count,
                note=note,
                sources=rule.sources,
            )
        )
    return tuple(out)

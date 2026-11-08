# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stage counts and the thirteen stop kinds of a floor line, each stop with its evidence (lean line board plan T5).

A task's stage is judged from its own evidence only: the pull requests it names, the PR watcher's record of them, the
running evidence and a live CLAIM. A recorded ``done``, a terminal outcome or a closed ticket proves no stage. A stop is
a reason a line is not moving; it is never progress, and it cannot be built without the rows or reads that show it.
The stop kinds, their order, their sentences and every source, outcome and bar named here are the contract's
``line_stops`` config; this module holds none of them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorRequest,
    ModelFloorLedgerRow,
    ModelFloorPr,
)
from omnimarket.models.model_floor_line_select import ModelFloorSourceHealth
from omnimarket.models.model_floor_line_stops import (
    ModelEvidenceRef,
    ModelFloorLineStopsConfig,
    ModelLineStop,
    ModelStageCount,
)
from omnimarket.nodes.node_factory_floor_compute.handlers.floor_lanes import pr_key
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_output import (
    ModelFloorBlocker,
    ModelFloorLine,
)

_RUN_ID_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2})(\d{2})Z$")


@dataclass(frozen=True)
class _Task:
    """One work task of a line: the tickets and pull requests that bind evidence to it."""

    task_id: str
    tickets: tuple[str, ...]
    prs: tuple[str, ...]


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_ref(row: ModelFloorLedgerRow) -> ModelEvidenceRef:
    return ModelEvidenceRef(
        kind="ledger-row",
        ref=f"{row.kind} {_stamp(row.ts)} lane={row.lane}",
        read_at=_stamp(row.ts),
    )


def _health_ref(entry: ModelFloorSourceHealth) -> ModelEvidenceRef:
    return ModelEvidenceRef(
        kind="source-health",
        ref=f"source-health {entry.source}",
        read_at=_stamp(entry.read_at) if entry.read_at is not None else "",
    )


def _cell(row: ModelFloorLedgerRow, key: str) -> str:
    return next((value for name, value in row.cells if name == key), "")


class _Board:
    """What the stop rules look up, read once per request."""

    def __init__(
        self,
        request: ModelFactoryFloorRequest,
        live_claim: Callable[
            [tuple[ModelFloorLedgerRow, ...]], ModelFloorLedgerRow | None
        ],
        running: frozenset[str] | None,
    ) -> None:
        self.request = request
        self.cfg: ModelFloorLineStopsConfig = request.config.line_stops
        self.live_claim = live_claim
        self.running = running
        self.now = request.generated_at
        self.rows: tuple[ModelFloorLedgerRow, ...] = tuple(
            dict.fromkeys((*request.ledger, *(request.ledger_unresolved or ())))
        )
        self.prs: dict[str, ModelFloorPr] = {
            pr_key(pr.key): pr for pr in request.watcher.prs
        }
        self.merged_at = {pr_key(m.key): m.merged_at for m in request.watcher.merges}
        self.health: dict[str, ModelFloorSourceHealth] = {
            entry.source: entry for entry in request.source_health
        }
        self.required: dict[str, frozenset[str]] | None = (
            None
            if request.required_contexts is None
            else {
                entry.repo.rsplit("/", 1)[-1]: frozenset(entry.contexts)
                for entry in request.required_contexts
            }
        )

    def state(self, pr: str) -> str:
        """OPEN, MERGED or CLOSED as the watcher records the pull request; empty when it is not in the watcher."""
        key = pr_key(pr)
        if key in self.merged_at:
            return "MERGED"
        record = self.prs.get(key)
        return record.state if record is not None else ""

    def unread(self, source: str) -> bool:
        entry = self.health.get(source)
        return entry is not None and entry.state == "unread"

    def sentence(self, kind: str, **facts: str) -> str:
        rule = next(r for r in self.cfg.stop_kinds if r.kind == kind)
        return rule.sentence.format(**facts)[: self.cfg.sentence_chars]

    def age(self, ts: datetime) -> str:
        minutes = max(int((self.now - ts).total_seconds() // 60), 0)
        return f"{minutes // 60} h" if minutes >= 120 else f"{minutes} min"

    def rows_for(
        self, tickets: tuple[str, ...], prs: tuple[str, ...]
    ) -> tuple[ModelFloorLedgerRow, ...]:
        wanted_tickets, wanted_prs = set(tickets), {pr_key(p) for p in prs}
        return tuple(
            row
            for row in self.rows
            if wanted_tickets.intersection(row.tickets)
            or wanted_prs.intersection(pr_key(p) for p in row.prs)
        )

    def cause_for(
        self,
        rows: tuple[ModelFloorLedgerRow, ...],
        skip: tuple[ModelFloorLedgerRow, ...],
    ) -> str:
        """The newest cause row among the rows that bear on the line: its kind, time, lane and words."""
        found: list[tuple[datetime, ModelFloorLedgerRow, str]] = []
        for row in rows:
            if row.kind not in self.cfg.cause_row_kinds or row in skip:
                continue
            words = next(
                (_cell(row, key) for key in self.cfg.cause_cells if _cell(row, key)),
                "",
            )
            if words:
                found.append((row.ts, row, words))
        if not found:
            return self.cfg.cause_missing
        ts, row, words = max(found, key=lambda f: (f[0], f[2]))
        return f"{row.kind} {_stamp(ts)} lane={row.lane}: {words}"[
            : self.cfg.sentence_chars
        ]


def _matches(row: ModelFloorLedgerRow, expression: str) -> bool:
    """Every ``;``-separated condition holds: ``key=value``, ``key~text`` (contains) or ``key>number``."""
    for part in (p.strip() for p in expression.split(";") if p.strip()):
        match = re.fullmatch(r"([A-Za-z_-]+)\s*([=~>])\s*(.*)", part)
        if match is None:
            return False
        key, op, want = match.groups()
        have = {
            "kind": row.kind,
            "lane": row.lane,
            "actor": row.actor,
            "outcome": row.outcome,
        }.get(key) or _cell(row, key)
        if op == "=" and have != want:
            return False
        if op == "~" and want not in have:
            return False
        if op == ">":
            number = re.match(r"-?\d+(\.\d+)?", have)
            if number is None or float(number.group()) <= float(want):
                return False
    return True


def _run_time(row: ModelFloorLedgerRow) -> datetime:
    match = _RUN_ID_TIME.match(row.run_id)
    if match is None:
        return row.ts
    day, hour, minute = match.groups()
    return datetime.fromisoformat(f"{day}T{hour}:{minute}:00+00:00")


def _tasks(
    line: ModelFloorLine, board: _Board, tickets: tuple[str, ...]
) -> tuple[_Task, ...]:
    """A plan line's tasks are its work items; any other line is one task, bound by the line's tickets."""
    gate = board.request.config.gate_kind
    if line.source in {"manifest", "plan-task-table"}:
        return tuple(
            _Task(item.id, item.tickets, item.prs)
            for station in line.stations
            for item in station.items
            if item.kind != gate
        )
    if not tickets:
        return ()
    named: dict[str, None] = {}
    for pr in board.request.watcher.prs:
        if set(tickets) & set(pr.tickets):
            named[pr_key(pr.key)] = None
    for merge in board.request.watcher.merges:
        if set(tickets) & set(merge.tickets):
            named[pr_key(merge.key)] = None
    for row in board.rows:
        if set(tickets) & set(row.tickets):
            for cited in row.prs:
                named[pr_key(cited)] = None
    return (_Task(line.line_id, tickets, tuple(named)),)


def _stage(task: _Task, board: _Board) -> str:
    cfg = board.cfg
    planned, building, pr_open, merged, running = cfg.stage_order
    states = [board.state(pr) for pr in task.prs]
    live = [
        (pr, state)
        for pr, state in zip(task.prs, states, strict=True)
        if state in {"OPEN", "MERGED"}
    ]
    needs_watcher = bool(task.prs)
    if (needs_watcher and board.unread(cfg.pr_watcher_source)) or (
        not task.prs and board.unread(cfg.ledger_source)
    ):
        return cfg.unread_stage
    if live and all(state == "MERGED" for _pr, state in live):
        if board.running is not None and all(
            pr_key(pr) in board.running for pr, _ in live
        ):
            return running
        return merged
    if any(state == "OPEN" for _pr, state in live):
        return pr_open
    claims = tuple(
        row
        for row in board.rows_for(task.tickets, ())
        if row.kind == board.request.config.line_select.claim_kind
    )
    return building if board.live_claim(claims) is not None else planned


def _more(count: int) -> str:
    return f" (+{count - 1} more)" if count > 1 else ""


def _contexts(pr: ModelFloorPr, failing: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(c.name for c in pr.contexts if c.result in failing)


def _context_refs(
    pr: ModelFloorPr, names: tuple[str, ...], read_at: str
) -> tuple[ModelEvidenceRef, ...]:
    return tuple(
        ModelEvidenceRef(
            kind="pr-context", ref=f"pr-context {pr.key} {name}", read_at=read_at
        )
        for name in names
    )


class _Stops:
    """The stops of one line, built in the contract's kind order."""

    def __init__(
        self,
        board: _Board,
        line: ModelFloorLine,
        tickets: tuple[str, ...],
        tasks: tuple[_Task, ...],
        stages: tuple[str, ...],
    ) -> None:
        self.board = board
        self.line = line
        self.tickets = tickets
        self.tasks = tasks
        self.stages = stages
        prs: dict[str, None] = {}
        for task in tasks:
            prs.update(dict.fromkeys(pr_key(p) for p in task.prs))
        self.prs = tuple(prs)
        self.rows = board.rows_for(tickets, self.prs)
        self.out: list[ModelLineStop] = []

    def add(
        self,
        kind: str,
        sentence: str,
        since: datetime | None,
        evidence: tuple[ModelEvidenceRef, ...],
        skip: tuple[ModelFloorLedgerRow, ...] = (),
        cause: bool = True,
    ) -> None:
        board = self.board
        self.out.append(
            ModelLineStop(
                kind=kind,
                sentence=sentence,
                since=_stamp(since) if since is not None else "",
                evidence=evidence,
                cause=board.cause_for(self.rows, skip) if cause else "",
            )
        )

    def open_prs(self) -> tuple[ModelFloorPr, ...]:
        return tuple(
            self.board.prs[p]
            for p in self.prs
            if p in self.board.prs and self.board.prs[p].state == "OPEN"
        )

    # --- one method per stop kind, in the contract's order -----------------------------------------------------

    def unread(self) -> None:
        board = self.board
        for source in board.cfg.unread_sources.get(self.line.line_class, ()):
            entry = board.health.get(source)
            if entry is None or entry.state not in {"unread", "stale"}:
                continue
            words = entry.cause or (
                f"older than its bar, read {_stamp(entry.read_at)}"
                if entry.read_at is not None
                else entry.state
            )
            self.add(
                "unread",
                board.sentence("unread", source=source, cause=words),
                entry.read_at,
                (_health_ref(entry),),
                cause=False,
            )

    def andon(self) -> None:
        board, line = self.board, self.line
        failing = board.cfg.failing_results
        watcher_read_at = _stamp(board.request.watcher.last_tick)
        if line.source in {"manifest", "plan-task-table", "undrawn"}:
            for text, kind, refs in zip(
                line.andon.causes,
                line.andon.cause_kinds,
                line.andon.cause_refs,
                strict=False,
            ):
                if kind == "lane-end":
                    continue
                evidence: list[ModelEvidenceRef] = []
                for ref in refs:
                    pr = board.prs.get(pr_key(ref))
                    row = next(
                        (r for r in board.rows if r.row_id and r.row_id == ref), None
                    )
                    if pr is not None:
                        evidence.extend(
                            _context_refs(pr, _contexts(pr, failing), watcher_read_at)
                        )
                        if not _contexts(pr, failing):
                            evidence.append(
                                ModelEvidenceRef(
                                    kind="pr-context",
                                    ref=f"pr-context {pr.key} ci",
                                    read_at=watcher_read_at,
                                )
                            )
                    elif row is not None:
                        evidence.append(_row_ref(row))
                if not evidence:
                    entry = board.health.get(board.cfg.ledger_source)
                    if entry is None:
                        continue
                    evidence.append(_health_ref(entry))
                self.add(
                    "andon",
                    board.sentence("andon", reason=text),
                    None,
                    tuple(dict.fromkeys(evidence)),
                )
            return
        for pr in self.open_prs():
            names = _contexts(pr, failing)
            if not (names or pr.red or pr.ci == "red"):
                continue
            evidence = list(_context_refs(pr, names, watcher_read_at)) or [
                ModelEvidenceRef(
                    kind="pr-context",
                    ref=f"pr-context {pr.key} ci",
                    read_at=watcher_read_at,
                )
            ]
            note = f" ({pr.ci_note})" if pr.ci_note else ""
            self.add(
                "andon",
                board.sentence("andon", reason=f"PR {pr.key} CI red{note}"),
                None,
                tuple(evidence),
            )

    def _terminals(self, outcomes: tuple[str, ...], kind: str) -> None:
        board = self.board
        for row in self.rows:
            if row.kind != "TERMINAL" or row.outcome not in outcomes:
                continue
            prs = {pr_key(p) for p in row.prs}
            if prs and all(board.state(p) == "MERGED" for p in prs):
                continue
            if self._resumed(row, prs):
                continue
            said = (row.reason or row.text).split(". ")[0]
            self.add(
                kind,
                board.sentence(
                    kind,
                    lane=row.lane,
                    age=board.age(row.ts),
                    said=said[: board.cfg.sentence_chars],
                ),
                row.ts,
                (_row_ref(row),),
                skip=(row,),
            )

    def _resumed(self, row: ModelFloorLedgerRow, prs: set[str]) -> bool:
        """A CLAIM naming the same task (its id or a pull request) that is live or ended done."""
        board = self.board
        # The work the row names has merged: every task of the line that carries one of its tickets is at merged or later.
        bound = [
            stage
            for task, stage in zip(self.tasks, self.stages, strict=True)
            if set(task.tickets) & set(row.tickets)
        ]
        if bound and all(stage in board.cfg.stage_order[3:] for stage in bound):
            return True
        for claim in self.rows:
            if (
                claim.kind != board.request.config.line_select.claim_kind
                or claim.ts <= row.ts
            ):
                continue
            same = (row.task and claim.task == row.task) or bool(
                prs & {pr_key(p) for p in claim.prs}
            )
            if not same:
                continue
            if board.live_claim((claim,)) is not None:
                return True
            if any(
                t.kind == "TERMINAL"
                and t.lane == claim.lane
                and t.ts >= claim.ts
                and t.outcome in board.cfg.done_outcomes
                for t in board.rows
            ):
                return True
        return False

    def lane_ended_blocked(self) -> None:
        self._terminals(self.board.cfg.blocked_outcomes, "lane-ended-blocked")

    def lane_ended_partial(self) -> None:
        self._terminals(self.board.cfg.partial_outcomes, "lane-ended-partial")

    def handed_off(self) -> None:
        board = self.board
        cfg = board.cfg
        if board.unread(cfg.pr_watcher_source):
            return
        for row in self.rows:
            handed_status = bool(_cell(row, cfg.handoff_status_cell))
            if not (
                (row.kind == "TERMINAL" and row.outcome in cfg.handoff_outcomes)
                or (row.kind == "STATUS" and handed_status)
            ):
                continue
            named = {pr_key(p) for p in row.prs}
            cell_value = _cell(row, cfg.handoff_status_cell)
            if "#" in cell_value:
                named.add(pr_key(cell_value.split()[0]))
            linked = named or {
                pr_key(p)
                for other in board.rows
                if other.lane == row.lane
                for p in other.prs
            }
            if linked and all(board.state(p) == "MERGED" for p in linked):
                continue
            if linked:
                where = ", ".join(
                    f"{p} { ({'OPEN': 'open', 'CLOSED': 'closed unmerged'}.get(board.state(p), 'not in the watcher')) }"
                    for p in sorted(linked)
                )
                shown = ",".join(sorted(linked))
            else:
                where, shown = "not in the watcher", "no pull request linked"
            self.add(
                "handed-off-unmerged",
                board.sentence(
                    "handed-off-unmerged", prs=shown, age=board.age(row.ts), where=where
                ),
                row.ts,
                (_row_ref(row),),
                skip=(row,),
            )

    def merged_not_running(self) -> None:
        board = self.board
        merged_stage = board.cfg.stage_order[3]
        behind = tuple(
            dict.fromkeys(
                pr_key(p)
                for task, stage in zip(self.tasks, self.stages, strict=True)
                if stage == merged_stage
                for p in task.prs
                if board.state(p) == "MERGED"
                and (board.running is None or pr_key(p) not in board.running)
            )
        )
        if not behind:
            return
        evidence: list[ModelEvidenceRef] = [
            ModelEvidenceRef(
                kind="merge",
                ref=f"merge {p}",
                read_at=_stamp(board.merged_at[p]) if p in board.merged_at else "",
            )
            for p in behind
        ]
        entry = board.health.get(board.cfg.running_source)
        if board.running is None and entry is not None:
            evidence.append(_health_ref(entry))
        elif board.running is not None:
            evidence.append(
                ModelEvidenceRef(
                    kind="running",
                    ref=f"running-evidence {behind[0]} absent",
                    read_at=_stamp(entry.read_at) if entry and entry.read_at else "",
                )
            )
        newest = min(
            (board.merged_at[p] for p in behind if p in board.merged_at), default=None
        )
        self.add(
            "merged-not-running",
            board.sentence(
                "merged-not-running",
                prs=behind[0],
                age=board.age(newest) if newest is not None else "an unread time",
                more=_more(len(behind)),
            ),
            newest,
            tuple(evidence),
        )

    def held_with_real_red(self) -> None:
        board = self.board
        held = {g.casefold() for g in board.request.held_gates}
        if not held or board.required is None:
            return
        failing = board.cfg.failing_results
        read_at = _stamp(board.request.watcher.last_tick)
        for pr in self.open_prs():
            names = _contexts(pr, failing)
            gates = tuple(n for n in names if n.casefold() in held)
            required = board.required.get(pr_key(pr.key).partition("#")[0], frozenset())
            real = tuple(n for n in names if n.casefold() not in held and n in required)
            if not gates or not real:
                continue
            self.add(
                "held-with-real-red",
                board.sentence(
                    "held-with-real-red", gate=gates[0], contexts=", ".join(real)
                ),
                None,
                _context_refs(pr, real, read_at),
            )

    def release_at_old_head(self) -> None:
        board = self.board
        cfg = board.cfg
        repos = {r.casefold() for r in board.request.release_required_repos}
        for pr in self.open_prs():
            if (
                pr_key(pr.key).partition("#")[0].casefold() not in repos
                or not pr.head_sha
            ):
                continue
            releases = [
                r
                for r in board.rows
                if r.kind == cfg.release_kind
                and r.head
                and pr_key(pr.key) in {pr_key(p) for p in r.prs}
            ]
            if not releases:
                continue
            newest = max(releases, key=lambda r: r.ts)
            if pr.head_sha.startswith(newest.head) or newest.head.startswith(
                pr.head_sha
            ):
                continue
            named = next(
                (
                    r
                    for r in sorted(board.rows, key=lambda r: r.ts)
                    if r.head
                    and (
                        pr.head_sha.startswith(r.head) or r.head.startswith(pr.head_sha)
                    )
                ),
                None,
            )
            moved = (
                f"head moved at {_stamp(named.ts)}"
                if named is not None
                else "head moved at an unread time"
            )
            self.add(
                "release-at-old-head",
                board.sentence(
                    "release-at-old-head",
                    old=newest.head[: cfg.head_chars],
                    written=_stamp(newest.ts),
                    head=pr.head_sha[: cfg.head_chars],
                    moved=moved,
                ),
                newest.ts,
                (_row_ref(newest),),
                skip=(newest,),
            )

    def person(self) -> None:
        board = self.board
        cfg = board.cfg
        roles = board.request.roles
        if not roles or board.unread(cfg.ledger_source):
            return
        for row in self.rows:
            if row.kind not in cfg.person_row_kinds:
                continue
            if row.kind == "TERMINAL" and row.outcome not in (
                *cfg.blocked_outcomes,
                *cfg.partial_outcomes,
            ):
                continue
            words = (row.reason or row.text).casefold()
            role = next((r for r in roles if r.casefold() in words), None)
            if role is None:
                continue
            ask = self._open_ask(role)
            if ask is not None:
                self.add(
                    "blocked-on-person",
                    board.sentence(
                        "blocked-on-person", role=role, since=_stamp(ask.ts)
                    ),
                    ask.ts,
                    (_row_ref(ask),),
                    skip=(ask,),
                )
            else:
                self.add(
                    "person-blocker-unverified",
                    board.sentence("person-blocker-unverified", role=role),
                    row.ts,
                    (_row_ref(row),),
                    skip=(row,),
                )

    def _open_ask(self, role: str) -> ModelFloorLedgerRow | None:
        """The newest ask to the role about this line's tickets or pull requests that nobody has answered."""
        board = self.board
        cfg = board.cfg
        asks = [
            r
            for r in self.rows
            if role.casefold() in {t.strip().casefold() for t in r.to.split(",")}
            and (
                r.kind == cfg.ask_kind
                or (
                    r.kind == "STATUS"
                    and _cell(r, "kind") == cfg.ask_status_kind
                    and r.state == cfg.ask_status_state
                )
            )
        ]
        open_asks = [a for a in asks if not self._answered(a, role)]
        return max(open_asks, key=lambda r: r.ts, default=None)

    def _answered(self, ask: ModelFloorLedgerRow, role: str) -> bool:
        cfg = self.board.cfg
        for row in self.board.rows:
            if row.ts <= ask.ts:
                continue
            if ask.row_id and row.re == ask.row_id and row.kind in cfg.answer_kinds:
                return True
            if row.lane.casefold() == role.casefold() and (
                set(row.tickets) & set(ask.tickets)
                or {pr_key(p) for p in row.prs} & {pr_key(p) for p in ask.prs}
            ):
                return True
        return False

    def ruled_not_started(self) -> None:
        board = self.board
        cfg = board.request.config.line_select
        ids = list(self.line.ruled_by)
        if self.line.line_class == "ruling":
            ids.append(self.line.line_id.split(":", 1)[-1])
        for ruling_id in dict.fromkeys(ids):
            ruling = next(
                (
                    r
                    for r in board.rows
                    if r.kind == cfg.ruling_kind
                    and (r.row_id or _stamp(r.ts)) == ruling_id
                ),
                None,
            )
            if ruling is None:
                continue
            citing = tuple(
                r
                for r in board.rows
                if r.ts >= ruling.ts
                and r.kind == board.cfg.ask_kind
                and (
                    ruling_id in r.ruling
                    or ruling_id in r.text
                    or ruling_id in r.reason
                )
            )
            # A CLAIM follows the ruling when it cites it, names its mechanism ticket, or comes from a lane (or on a
            # ticket) that a row citing the ruling routed the work to. Rows about other work do not clear it.
            followed = any(
                c.kind == cfg.claim_kind
                and c.ts > ruling.ts
                and (
                    (ruling.mechanism and ruling.mechanism in c.tickets)
                    or ruling_id in c.ruling
                    or ruling_id in c.text
                    or ruling_id in c.reason
                    or any(
                        c.lane in {t.strip() for t in r.to.split(",")}
                        or set(r.tickets) & set(c.tickets)
                        for r in citing
                        if r.ts <= c.ts
                    )
                )
                for c in board.rows
            )
            if followed:
                continue
            self.add(
                "ruled-not-started",
                board.sentence("ruled-not-started", ruling=ruling_id),
                ruling.ts,
                (_row_ref(ruling),),
                skip=(ruling,),
            )

    def process_did_nothing(self) -> None:
        board = self.board
        cfg = board.cfg
        if self.line.line_class != "process":
            return
        name = self.line.line_id.split(":", 1)[-1]
        decl = next((d for d in board.request.processes if d.name == name), None)
        if (
            decl is None
            or not decl.run_kind
            or not decl.did_nothing_match
            or decl.interval_min <= 0
        ):
            return
        by_run: dict[str, ModelFloorLedgerRow] = {}
        for row in sorted(board.rows, key=lambda r: r.ts):
            # Every row of the process's run record is a run, whatever it says: a run summary ends a chain of idle runs.
            if row.run_id and _matches(row, decl.run_match):
                by_run[row.run_id] = row
        runs = sorted(by_run.values(), key=_run_time)
        chain: list[ModelFloorLedgerRow] = []
        for row in reversed(runs):
            if row.kind != decl.run_kind or not _matches(row, decl.did_nothing_match):
                break
            if chain and (_run_time(chain[-1]) - _run_time(row)) > timedelta(
                minutes=decl.interval_min * cfg.run_gap_factor
            ):
                break
            chain.append(row)
        if len(chain) < cfg.process_did_nothing_runs:
            return
        chain.reverse()
        first = _run_time(chain[0])
        detail = ", ".join(
            f"{key}={_cell(chain[-1], key)}"
            for key in re.findall(r"([A-Za-z_-]+)\s*>", decl.did_nothing_match)
            if _cell(chain[-1], key)
        )
        self.add(
            "process-did-nothing",
            board.sentence(
                "process-did-nothing",
                runs=str(len(chain)),
                first=_stamp(first),
                detail=detail or decl.did_nothing_match,
            ),
            first,
            tuple(_row_ref(r) for r in chain),
            skip=tuple(chain),
        )

    def idle(self) -> None:
        board = self.board
        cfg = board.cfg
        if (
            self.line.line_class in cfg.idle_ignored_classes
            or self.line.owner
            or board.health.get(cfg.ledger_source) is None
            or board.unread(cfg.ledger_source)
            or (self.stages and all(s == cfg.stage_order[4] for s in self.stages))
        ):
            return
        if not self.rows:
            return
        newest = max(self.rows, key=lambda r: r.ts)
        if board.now - newest.ts < timedelta(hours=cfg.idle_hours):
            return
        self.add(
            "idle",
            board.sentence(
                "idle", hours=str(int((board.now - newest.ts).total_seconds() // 3600))
            ),
            newest.ts,
            (_row_ref(newest),),
            skip=(newest,),
        )

    def build(self) -> tuple[ModelLineStop, ...]:
        self.unread()
        self.andon()
        self.lane_ended_blocked()
        self.lane_ended_partial()
        self.handed_off()
        self.merged_not_running()
        self.held_with_real_red()
        self.release_at_old_head()
        self.person()
        self.ruled_not_started()
        self.process_did_nothing()
        self.idle()
        order = [rule.kind for rule in self.board.cfg.stop_kinds]
        return tuple(sorted(self.out, key=lambda stop: order.index(stop.kind)))


def _line_tickets(
    line: ModelFloorLine,
    scopes: dict[str, tuple[str, ...]],
    request: ModelFactoryFloorRequest,
) -> tuple[str, ...]:
    """The tickets a line's evidence binds through: a plan's scope, a ticket line's own, a goal row's, a ruling's."""
    if line.line_id in scopes:
        item_tickets = tuple(
            t for st in line.stations for item in st.items for t in item.tickets
        )
        return tuple(dict.fromkeys((*scopes[line.line_id], *item_tickets)))
    prefixes = {r.line_class: r.id_prefix for r in request.config.line_select.classes}
    bare = line.line_id.removeprefix(prefixes.get(line.line_class, ""))
    if line.line_class == "ticket":
        return (bare,)
    if line.line_class == "goal":
        goal = next((g for g in request.goal_rows or () if g.row_id == bare), None)
        return goal.tickets if goal is not None else ()
    if line.line_class == "ruling":
        kind = request.config.line_select.ruling_kind
        row = next(
            (
                r
                for r in request.ledger
                if r.kind == kind and (r.row_id or _stamp(r.ts)) == bare
            ),
            None,
        )
        return row.tickets if row is not None else ()
    return ()


def apply_line_board(
    request: ModelFactoryFloorRequest,
    lines: tuple[ModelFloorLine, ...],
    scopes: dict[str, tuple[str, ...]],
    live_claim: Callable[[tuple[ModelFloorLedgerRow, ...]], ModelFloorLedgerRow | None],
    running: frozenset[str] | None,
) -> tuple[ModelFloorLine, ...]:
    """Each line with its stage counts, owner and stops; a line with a stop beyond a soft andon reads stopped."""
    board = _Board(request, live_claim, running)
    cfg = board.cfg
    out: list[ModelFloorLine] = []
    for line in lines:
        tickets = _line_tickets(line, scopes, request)
        tasks = _tasks(line, board, tickets)
        stages = tuple(_stage(task, board) for task in tasks)
        claims = tuple(
            r
            for r in board.rows_for(tickets, ())
            if r.kind == request.config.line_select.claim_kind
        )
        held = live_claim(claims)
        owner = held.lane if held is not None else ""
        counts = tuple(
            ModelStageCount(stage=stage, count=stages.count(stage))
            for stage in (*cfg.stage_order, cfg.unread_stage)
        )
        owned = line.model_copy(update={"owner": owner, "stage_counts": counts})
        try:
            stops = _Stops(board, owned, tickets, tasks, stages).build()
        except ValidationError as error:
            raise ValueError(
                f"line {line.line_id}: a stop was built without evidence"
            ) from error
        update: dict[str, object] = {"stops": stops}
        if stops and any(stop.kind != "andon" for stop in stops):
            update["condition"] = "stopped"
            if line.blocker is None:
                update["blocker"] = ModelFloorBlocker(
                    reason=stops[0].sentence,
                    evidence=tuple(ref.ref for ref in stops[0].evidence),
                )
        out.append(owned.model_copy(update=update))
    return tuple(out)

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Name, per factory-floor line, the ready tasks a lane may be started for.

A pure compute: no clock and no I/O. ``now``, the floor record text, the per-task payload verdicts, the per-task
entry-ticket verdicts and the fill's state entries all come in on the request. The pill of a line is never read.

A task is ready when its start criterion reads true (the prerequisites of its ``Start when:`` line all read true),
it is not done, active or stopped, it has no pull request, no live lane names it and its key is not held by a RESERVED
entry or a SENT entry inside the window. Nothing an operator says is read: the request has no field for it, and the
floor record's own ``wait`` state and note do not decide readiness. A task with an unmet prerequisite is not ready and
names it; a task with no criterion, or one that cannot be parsed, is not ready and names that. Every ready task is
returned in record order with no cutoff by capacity (the reducer's reserve rule is the one capacity gate), so a task
whose ticket is refused cannot hide the next one.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import ValidationError

from omnimarket.models.floor_fill import (
    ModelFloorEntryTicketVerdict,
    ModelFloorFillCandidate,
    ModelFloorFillCandidatesRequest,
    ModelFloorFillCandidatesResult,
    ModelFloorFillLine,
    ModelFloorFillNotReady,
    ModelFloorFillStateEntry,
    ModelFloorFillStopped,
    ModelFloorFillStoppedNotReady,
    ModelFloorFillUnticketable,
    ModelFloorRecordRead,
    ModelFloorTaskPayloadVerdict,
    parse_stamp,
)
from omnimarket.models.floor_fill.model_floor_fill_candidates import (
    NEEDS_UNPARSED,
    STOPPED_KEY_PREFIX,
    STOPPED_LINE,
    ModelRecordItem,
    ModelRecordLine,
    ModelRecordStopped,
    StoppedVerdict,
    UnreadCause,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.start_criterion import (
    PrerequisiteReader,
    parse_start_when,
)

KEY_PREFIX = "floor:"
LANE_KIND = "lane"
NO_CRITERION = "no-start-criterion"
UNPARSEABLE = "start-when-unparseable:"
UNMET = "unmet:"

Pair = tuple[str, str]


def floor_key(line_id: str, task_id: str) -> str:
    """The key a floor lane is held under: ``floor:<line_id>:<task_id>``."""
    return f"{KEY_PREFIX}{line_id}:{task_id}"


def stopped_key(lane: str) -> str:
    """The key a stopped lane's continuation is held under: ``stopped:<lane>``."""
    return f"{STOPPED_KEY_PREFIX}{lane}"


def _split_key(key: str) -> Pair | None:
    if not key.startswith(KEY_PREFIX):
        return None
    line_id, _, task_id = key[len(KEY_PREFIX) :].rpartition(":")
    return (line_id, task_id) if line_id and task_id else None


class HandlerFloorFillCandidates:
    """Compute the ready tasks of every line of the floor record, or one FLOOR-UNREAD for the whole floor."""

    def handle(
        self, request: ModelFloorFillCandidatesRequest
    ) -> ModelFloorFillCandidatesResult:
        record, cause = self._read(request)
        if record is None:
            return ModelFloorFillCandidatesResult(
                verdict="FLOOR-UNREAD", unread_cause=cause
            )
        view = _View(request, record)
        stopped, not_ready, capacity_left = view.stopped_lanes(record)
        verdict: StoppedVerdict = (
            "STARTABLE"
            if stopped
            else "AT-CAPACITY"
            if capacity_left == 0 and view.stopped_ready
            else "NO-STOPPED"
        )
        return ModelFloorFillCandidatesResult(
            verdict="READ",
            lines=tuple(view.line(line) for line in record.lines),
            stopped_verdict=verdict,
            stopped_capacity_left=capacity_left,
            stopped=stopped,
            stopped_not_ready=not_ready,
        )

    @staticmethod
    def _read(
        request: ModelFloorFillCandidatesRequest,
    ) -> tuple[ModelFloorRecordRead | None, UnreadCause]:
        text = request.floor_record
        if text is None or not text.strip():
            return None, "absent"
        try:
            record = ModelFloorRecordRead.model_validate_json(text)
            generated_at = parse_stamp(record.generated_at)
        except (ValidationError, ValueError):
            return None, "malformed"
        age = parse_stamp(request.now) - generated_at
        if age > timedelta(minutes=request.max_age_minutes):
            return None, "old"
        return record, ""


class _View:
    """The request and the record, indexed once, for the per-line decisions."""

    def __init__(
        self, request: ModelFloorFillCandidatesRequest, record: ModelFloorRecordRead
    ) -> None:
        self.request = request
        now = parse_stamp(request.now)
        window = timedelta(minutes=request.sent_window_minutes)
        self.held: dict[Pair, str] = {}
        self.consuming: dict[str, int] = {}
        self.stopped_held: dict[str, str] = {}
        self.stopped_consuming = 0
        # Stopped lanes that are ready (held and capacity aside); a held or live one is not.
        self.stopped_ready = 0
        for entry in request.entries:
            if entry.key.startswith(STOPPED_KEY_PREFIX):
                name = entry.key[len(STOPPED_KEY_PREFIX) :]
                if (
                    self._holds(entry, now, window)
                    and self.stopped_held.get(name) != "RESERVED"
                ):
                    self.stopped_held[name] = entry.status
                if entry.status == "RESERVED" or not entry.ended:
                    self.stopped_consuming += 1
                continue
            pair = _split_key(entry.key)
            if pair is None:
                continue
            # A RESERVED entry outranks a SENT one on the same key.
            if self._holds(entry, now, window) and self.held.get(pair) != "RESERVED":
                self.held[pair] = entry.status
            if entry.status == "RESERVED" or not entry.ended:
                self.consuming[pair[0]] = self.consuming.get(pair[0], 0) + 1
        self.lanes: dict[Pair, str] = {}
        for lane in record.floor.lanes:
            if lane.station is not None:
                self.lanes.setdefault(
                    (lane.station.line_id, lane.station.item_id), lane.lane
                )
        self.tickets: dict[Pair, ModelFloorEntryTicketVerdict] = {
            (v.line_id, v.task_id): v for v in request.ticket_verdicts
        }
        self.payloads: dict[Pair, ModelFloorTaskPayloadVerdict] = {
            (v.line_id, v.task_id): v for v in request.payload_verdicts
        }
        self.criteria: dict[Pair, str] = {
            (c.line_id, c.task_id): c.start_when for c in request.start_criteria
        }
        self.reader = PrerequisiteReader(
            now=request.now,
            pieces_record=request.pieces_record,
            merged_refs=request.merged_refs,
            m0_verdict=request.m0_verdict,
        )

    def stopped_lanes(
        self, record: ModelFloorRecordRead
    ) -> tuple[
        tuple[ModelFloorFillStopped, ...],
        tuple[ModelFloorFillStoppedNotReady, ...],
        int,
    ]:
        """The stopped lanes of the record that may be continued, those that may not with their cause, and room left."""
        live = {lane.lane for lane in record.floor.lanes}
        live_tickets = {lane.ticket for lane in record.floor.lanes if lane.ticket}
        reports = {r.lane: r.text for r in self.request.reports}
        ready: list[ModelFloorFillStopped] = []
        refused: list[ModelFloorFillStoppedNotReady] = []
        for entry in record.floor.stopped:
            cause = self._stopped_not_ready_cause(entry, live, live_tickets)
            if cause:
                refused.append(
                    ModelFloorFillStoppedNotReady(lane=entry.lane, cause=cause)
                )
                continue
            verdict = self.tickets.get((STOPPED_LINE, entry.lane))
            ticket = (
                verdict.ticket
                if verdict is not None and verdict.status == "usable"
                else entry.ticket
            )
            ready.append(
                ModelFloorFillStopped(
                    key=stopped_key(entry.lane),
                    lane=entry.lane,
                    ticket=ticket,
                    branch=entry.branch,
                    head=entry.head,
                    brief=self._brief(entry, ticket, reports.get(entry.lane)),
                )
            )
        self.stopped_ready = len(ready)
        capacity_left = max(0, self.request.stopped_max_lanes - self.stopped_consuming)
        return (tuple(ready) if capacity_left else ()), tuple(refused), capacity_left

    def _stopped_not_ready_cause(
        self, entry: ModelRecordStopped, live: set[str], live_tickets: set[str]
    ) -> str:
        if entry.lane in live:
            return f"live-lane:{entry.lane}"
        shared = sorted(live_tickets & set(entry.tickets))
        if shared:
            return f"live-ticket:{shared[0]}"
        held = self.stopped_held.get(entry.lane)
        if held is not None:
            return f"held:{held}"
        verdict = self.tickets.get((STOPPED_LINE, entry.lane))
        if verdict is not None and verdict.status == "refused":
            return f"ticket-refused:{verdict.refusal}"
        if verdict is not None and verdict.status == "unreadable":
            return "ticket-unreadable"
        if not entry.ticket and not (
            verdict is not None and verdict.status == "usable"
        ):
            return "no-ticket"
        return ""

    def _brief(self, entry: ModelRecordStopped, ticket: str, report: str | None) -> str:
        request = self.request
        named = ", ".join(entry.names) or "nothing"
        where = f"branch {entry.branch}" if entry.branch else "no branch named"
        if entry.head:
            where += f" head {entry.head}"
        if entry.prs:
            where += f"; pull requests {', '.join(entry.prs)}"
        rows = "\n".join(f"- {row}" for row in entry.rows) or "- (no rows were carried)"
        # The closing row's typed ask; an ``unparsed`` cell names none, so the brief does not carry it.
        asks = (
            f"\n\nThe predecessor asked for next: {entry.needs}"
            if entry.needs and entry.needs != NEEDS_UNPARSED
            else ""
        )
        if report is None:
            said = (
                f"not supplied; read {entry.report_path} if it exists"
                if entry.report_path
                else "none"
            )
        else:
            said = report[: request.stopped_report_chars]
        return (
            f"Continue lane {entry.lane} on ticket {ticket}. It ended {entry.outcome} at {entry.ended_at}, naming {named}; "
            f"{where}.{asks}\n\nThe predecessor's last rows:\n{rows}\n\nThe predecessor's report ({entry.report_path or 'no path'}):\n"
            f"{said}\n\nRule: {request.stopped_continue_rule}"
        )

    @staticmethod
    def _holds(
        entry: ModelFloorFillStateEntry, now: datetime, window: timedelta
    ) -> bool:
        if entry.status == "RESERVED":
            return True
        return parse_stamp(entry.at) + window > now

    def line(self, line: ModelRecordLine) -> ModelFloorFillLine:
        candidates: list[ModelFloorFillCandidate] = []
        untickettable: list[ModelFloorFillUnticketable] = []
        not_ready: list[ModelFloorFillNotReady] = []
        for station in line.stations:
            for item in station.items:
                refusal = self._not_ready(line.line_id, item)
                if refusal is not None:
                    not_ready.append(refusal)
                    continue
                self._ready(line.line_id, item.id, candidates, untickettable)
        capacity_left = max(
            0,
            self.request.max_fill_lanes_per_line - self.consuming.get(line.line_id, 0),
        )
        if candidates and capacity_left == 0:
            verdict = "AT-CAPACITY"
            candidates = []
        elif candidates:
            verdict = candidates[0].verdict
        elif untickettable:
            verdict = "UNTICKETABLE"
        else:
            verdict = "NO-STARTABLE"
        return ModelFloorFillLine(
            line_id=line.line_id,
            verdict=verdict,
            capacity_left=capacity_left,
            candidates=tuple(candidates),
            untickettable=tuple(untickettable),
            not_ready=tuple(not_ready),
        )

    def _not_ready(
        self, line_id: str, item: ModelRecordItem
    ) -> ModelFloorFillNotReady | None:
        cause = self._occupied_cause(line_id, item)
        if cause:
            return ModelFloorFillNotReady(task_id=item.id, cause=cause)
        return self._criterion_refusal(line_id, item.id)

    def _occupied_cause(self, line_id: str, item: ModelRecordItem) -> str:
        if item.state == "done":
            return "done"
        if item.kind != LANE_KIND:
            return f"not-a-lane:{item.kind}"
        if item.state in ("active", "stop"):
            return item.state
        if item.prs:
            return "open-pr:" + ",".join(item.prs)
        lane = self.lanes.get((line_id, item.id))
        if lane is not None:
            return f"live-lane:{lane}"
        held = self.held.get((line_id, item.id))
        if held is not None:
            return f"held:{held}"
        return ""

    def _criterion_refusal(
        self, line_id: str, task_id: str
    ) -> ModelFloorFillNotReady | None:
        """None when the task's start criterion reads true, otherwise the refusal naming what is unmet."""
        text = self.criteria.get((line_id, task_id))
        if text is None:
            return ModelFloorFillNotReady(task_id=task_id, cause=NO_CRITERION)
        parse = parse_start_when(text)
        if parse.unparseable_cause:
            return ModelFloorFillNotReady(
                task_id=task_id, cause=UNPARSEABLE + parse.unparseable_cause
            )
        unmet = tuple(
            f"{p.label()} ({why})"
            for p in parse.prerequisites
            if (why := self.reader.unmet(p))
        )
        if not unmet:
            return None
        return ModelFloorFillNotReady(
            task_id=task_id, cause=UNMET + "; ".join(unmet), unmet=unmet
        )

    def _ready(
        self,
        line_id: str,
        task_id: str,
        candidates: list[ModelFloorFillCandidate],
        untickettable: list[ModelFloorFillUnticketable],
    ) -> None:
        key = floor_key(line_id, task_id)
        ticket = self.tickets.get((line_id, task_id))
        status = "none" if ticket is None else ticket.status
        if ticket is not None and status == "usable":
            candidates.append(
                ModelFloorFillCandidate(
                    key=key,
                    task_id=task_id,
                    verdict="STARTABLE",
                    ticket=ticket.ticket,
                )
            )
            return
        if ticket is not None and status == "refused":
            cause = f"ticket-refused:{ticket.refusal}"
        elif status == "unreadable":
            cause = "ticket-unreadable"
        else:
            payload = self.payloads.get((line_id, task_id))
            if payload is None:
                cause = "plan-unread"
            elif payload.ok:
                candidates.append(
                    ModelFloorFillCandidate(
                        key=key, task_id=task_id, verdict="NEEDS-TICKET"
                    )
                )
                return
            else:
                cause = payload.cause
        untickettable.append(
            ModelFloorFillUnticketable(key=key, task_id=task_id, cause=cause)
        )

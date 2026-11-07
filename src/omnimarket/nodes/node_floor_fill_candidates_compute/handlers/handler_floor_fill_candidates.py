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

from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_start_criterion import (
    PrerequisiteReader,
    parse_start_when,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.models import (
    ModelFloorEntryTicketVerdict,
    ModelFloorFillCandidate,
    ModelFloorFillCandidatesRequest,
    ModelFloorFillCandidatesResult,
    ModelFloorFillLine,
    ModelFloorFillNotReady,
    ModelFloorFillStateEntry,
    ModelFloorFillUnticketable,
    ModelFloorRecordRead,
    ModelFloorTaskPayloadVerdict,
    parse_stamp,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.models.model_floor_fill_candidates import (
    ModelRecordItem,
    ModelRecordLine,
    UnreadCause,
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
        return ModelFloorFillCandidatesResult(
            verdict="READ", lines=tuple(view.line(line) for line in record.lines)
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
        for entry in request.entries:
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

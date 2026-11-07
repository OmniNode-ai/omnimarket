# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Name, per factory-floor line, the ready tasks a lane may be started for.

A pure compute: no clock and no I/O. ``now``, the floor record text, the per-task payload verdicts, the per-task
entry-ticket verdicts and the fill's state entries all come in on the request. The pill of a line is never read.

A task is ready when its state is ``prep``, its note starts ``ready``, it has no pull request, no live lane names it
and its key is not held by a RESERVED entry or a SENT entry inside the window. Every ready task is returned in record
order with no cutoff by capacity (the reducer's reserve rule is the one capacity gate), so a task whose ticket is
refused cannot hide the next one.
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
    ModelFloorFillUnticketable,
    ModelFloorRecordRead,
    ModelFloorTaskPayloadVerdict,
    parse_stamp,
)
from omnimarket.models.floor_fill.model_floor_fill_candidates import (
    ModelRecordItem,
    ModelRecordLine,
    UnreadCause,
)

KEY_PREFIX = "floor:"
WAITS_FOR = "waits for "
READY_NOTE = "ready"
LANE_KIND = "lane"

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
                cause = self._not_ready_cause(line.line_id, item)
                if cause:
                    not_ready.append(
                        ModelFloorFillNotReady(task_id=item.id, cause=cause)
                    )
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

    def _not_ready_cause(self, line_id: str, item: ModelRecordItem) -> str:
        if item.state == "done":
            return "done"
        if item.kind != LANE_KIND:
            return f"not-a-lane:{item.kind}"
        if item.state == "wait":
            note = item.note.strip()
            return "wait:" + (
                note.removeprefix(WAITS_FOR).strip()
                if note.startswith(WAITS_FOR)
                else note
            )
        if item.state in ("active", "stop"):
            return item.state
        if not item.note.startswith(READY_NOTE):
            return f"not-ready:{item.note}"
        if item.prs:
            return "open-pr:" + ",".join(item.prs)
        lane = self.lanes.get((line_id, item.id))
        if lane is not None:
            return f"live-lane:{lane}"
        held = self.held.get((line_id, item.id))
        if held is not None:
            return f"held:{held}"
        return ""

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

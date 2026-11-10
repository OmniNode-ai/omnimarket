# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDispatchReading: lanes with no dispatch record, and dispatch records with no terminal result inside their deadline.

COMPUTE handler: no clock read (the request carries the time), no state, no I/O. A lane or record side that was not
read, an empty read and a read older than its max age read UNKNOWN and never WORKING.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_dispatch_reading_compute.models import (
    ModelDispatchReadingRequest,
    ModelDispatchReadingResult,
    ModelDispatchRecord,
    ModelLaneFact,
    ModelNamedLane,
    ModelReadingSubject,
    ModelUnreconciledDispatch,
)

SUBJECT_READ = "dispatch_read"
SUBJECT_LANES_WITHOUT_RECORD = "lanes_without_dispatch_record"
SUBJECT_RECONCILIATION = "dispatch_reconciliation"
SUBJECT_READ_AGE = "read_age"

_W = EnumReadingVerdict.WORKING
_N = EnumReadingVerdict.NOT_WORKING
_U = EnumReadingVerdict.UNKNOWN


class HandlerDispatchReading:
    """handle(request) -> reading; subjects are the lanes, then the reconciliation, then the read age."""

    def handle(
        self, request: ModelDispatchReadingRequest
    ) -> ModelDispatchReadingResult:
        lanes = request.lanes
        records = request.records
        if request.read_failure is not None or lanes is None or records is None:
            why = (
                request.read_failure.value
                if request.read_failure is not None
                else "lanes-not-read"
                if lanes is None
                else "records-not-read"
            )
            return self._unread(
                request, f"the lanes and dispatch records were not read: {why}"
            )
        if not lanes and not records:
            return self._unread(request, "no lane and no dispatch record was returned")

        unrecorded = self._lanes_without_record(request, lanes, records)
        unreconciled = self._unreconciled(request, records)
        subjects = (
            self._lanes_subject(len(lanes), unrecorded),
            self._reconciliation_subject(len(records), unreconciled),
            self._read_age(request),
        )
        if subjects[-1].verdict is _U:
            overall = _U
        elif any(s.verdict is _N for s in subjects):
            overall = _N
        elif any(s.verdict is _U for s in subjects):
            overall = _U
        else:
            overall = _W
        return ModelDispatchReadingResult(
            correlation_id=request.correlation_id,
            verdict=overall,
            subjects=subjects,
            lanes_read=len(lanes),
            records_read=len(records),
            lanes_without_record=unrecorded,
            unreconciled=unreconciled,
        )

    @staticmethod
    def _unread(
        request: ModelDispatchReadingRequest, reason: str
    ) -> ModelDispatchReadingResult:
        return ModelDispatchReadingResult(
            correlation_id=request.correlation_id,
            verdict=_U,
            subjects=(
                ModelReadingSubject(name=SUBJECT_READ, verdict=_U, reason=reason),
            ),
            lanes_read=len(request.lanes or ()),
            records_read=len(request.records or ()),
            read_failure=request.read_failure,
        )

    @staticmethod
    def _from_scratch(lane: ModelLaneFact, markers: tuple[str, ...]) -> bool:
        """AC1: the lane's launching command or script path carries a scratch-script marker."""
        source = lane.launched_from
        return source is not None and any(marker in source for marker in markers)

    def _lanes_without_record(
        self,
        request: ModelDispatchReadingRequest,
        lanes: tuple[ModelLaneFact, ...],
        records: tuple[ModelDispatchRecord, ...],
    ) -> tuple[ModelNamedLane, ...]:
        recorded = {record.lane for record in records}
        return tuple(
            ModelNamedLane(
                lane=lane.lane,
                kind=lane.kind,
                launched_from=lane.launched_from,
                from_scratch_script=self._from_scratch(
                    lane, request.settings.scratch_script_markers
                ),
            )
            for lane in sorted(lanes, key=lambda item: item.lane)
            if lane.lane not in recorded
        )

    def _unreconciled_reason(
        self, record: ModelDispatchRecord, now_past_deadline: bool
    ) -> str | None:
        """AC2: a record with no terminal result at its deadline, or one whose terminal result came after it."""
        terminal = record.terminal
        if terminal is None:
            return "no terminal result at its deadline" if now_past_deadline else None
        if terminal.at > record.deadline_at:
            late = int((terminal.at - record.deadline_at).total_seconds())
            return f"terminal result {terminal.outcome} arrived {late} s after its deadline"
        return None

    def _unreconciled(
        self,
        request: ModelDispatchReadingRequest,
        records: tuple[ModelDispatchRecord, ...],
    ) -> tuple[ModelUnreconciledDispatch, ...]:
        named: list[ModelUnreconciledDispatch] = []
        for record in sorted(records, key=lambda item: item.dispatch_id):
            reason = self._unreconciled_reason(record, request.now > record.deadline_at)
            if reason is not None:
                named.append(
                    ModelUnreconciledDispatch(
                        dispatch_id=record.dispatch_id, lane=record.lane, reason=reason
                    )
                )
        return tuple(named)

    @staticmethod
    def _lanes_subject(
        lanes_read: int, unrecorded: tuple[ModelNamedLane, ...]
    ) -> ModelReadingSubject:
        if not unrecorded:
            return ModelReadingSubject(
                name=SUBJECT_LANES_WITHOUT_RECORD,
                verdict=_W,
                count=0,
                reason=f"{lanes_read} lane(s) read, every one has a dispatch record",
            )
        scratch = [lane for lane in unrecorded if lane.from_scratch_script]
        naming = ", ".join(
            f"{lane.lane} (started from scratch script {lane.launched_from})"
            if lane.from_scratch_script
            else lane.lane
            for lane in unrecorded
        )
        return ModelReadingSubject(
            name=SUBJECT_LANES_WITHOUT_RECORD,
            verdict=_N,
            count=len(unrecorded),
            reason=(
                f"{len(unrecorded)} of {lanes_read} lane(s) have no dispatch record, "
                f"{len(scratch)} started from a scratch script: {naming}"
            ),
        )

    @staticmethod
    def _reconciliation_subject(
        records_read: int, unreconciled: tuple[ModelUnreconciledDispatch, ...]
    ) -> ModelReadingSubject:
        if not unreconciled:
            return ModelReadingSubject(
                name=SUBJECT_RECONCILIATION,
                verdict=_W,
                count=0,
                reason=f"{records_read} dispatch record(s) read, none past its deadline without a terminal result",
            )
        naming = "; ".join(
            f"{item.dispatch_id} ({item.lane}): {item.reason}" for item in unreconciled
        )
        return ModelReadingSubject(
            name=SUBJECT_RECONCILIATION,
            verdict=_N,
            count=len(unreconciled),
            reason=f"{len(unreconciled)} of {records_read} dispatch record(s) do not reconcile: {naming}",
        )

    @staticmethod
    def _read_age(request: ModelDispatchReadingRequest) -> ModelReadingSubject:
        elapsed = request.now - request.read_at
        limit = request.settings.max_age_seconds
        age = int(elapsed.total_seconds())
        if elapsed < timedelta(0):
            return ModelReadingSubject(
                name=SUBJECT_READ_AGE,
                verdict=_U,
                reason="the read is dated after the time it is judged at",
            )
        if elapsed > timedelta(seconds=limit):
            return ModelReadingSubject(
                name=SUBJECT_READ_AGE,
                verdict=_U,
                count=age,
                reason=f"the read is {age} s old, past the {limit} s limit",
            )
        return ModelReadingSubject(
            name=SUBJECT_READ_AGE,
            verdict=_W,
            count=age,
            reason=f"the read is {age} s old, inside the {limit} s limit",
        )


__all__: list[str] = [
    "SUBJECT_LANES_WITHOUT_RECORD",
    "SUBJECT_READ",
    "SUBJECT_READ_AGE",
    "SUBJECT_RECONCILIATION",
    "HandlerDispatchReading",
]

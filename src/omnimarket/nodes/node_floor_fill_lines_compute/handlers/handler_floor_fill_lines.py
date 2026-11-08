# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick's lines for the stopped-line fill: each untouched startable line is named.

Pure: the pass (T13, step S6) reads everything it needs and hands it over; this handler
reads no clock and does no I/O. It words what the pass did, in the finding shape of the
tick, and decides nothing the reducer decided.
"""

from __future__ import annotations

from omnimarket.nodes.node_floor_fill_lines_compute.models import (
    EnumFloorFillLinesEntryState,
    EnumFloorFillLinesLevel,
    EnumFloorFillLinesSource,
    EnumFloorFillLinesStartKind,
    EnumFloorFillLinesVerdict,
    ModelFloorFillLinesEntry,
    ModelFloorFillLinesFinding,
    ModelFloorFillLinesLine,
    ModelFloorFillLinesRequest,
    ModelFloorFillLinesResult,
    ModelFloorFillLinesStartResult,
    line_of_key,
)

_L = EnumFloorFillLinesLevel
_V = EnumFloorFillLinesVerdict
_K = EnumFloorFillLinesStartKind

_UNREAD_FIX: dict[EnumFloorFillLinesSource, str] = {
    EnumFloorFillLinesSource.FLOOR: (
        "read the published floor record by hand; the pass stopped and starts no lane "
        "from a floor it could not read"
    ),
    EnumFloorFillLinesSource.POOL: (
        "read the lab host pool by hand; the pass stopped and starts no lane without a "
        "placement it could read"
    ),
    EnumFloorFillLinesSource.BRAKE: (
        "read the open-count brake by hand; the pass stopped and never assumes it is off"
    ),
    EnumFloorFillLinesSource.STOP_SIGNAL: (
        "read the stop signals by hand; the pass stopped and never assumes none holds"
    ),
    EnumFloorFillLinesSource.LEDGER: (
        "read the rolling ledger by hand; the pass stopped and never guesses a CLAIM"
    ),
}
_STOPPED_FIX = "the next tick retries the start; if the cause repeats, fix the cause named in the line"
_TICKET_FIX = (
    "give the task what its cause names (a spec, a parent, a usable ticket); "
    "the line has nothing else to start"
)
_RESERVED_FIX = (
    "the ledger could not be read for this reservation; read the CLAIM by hand "
    "or wait for the next tick to resolve it"
)


def _finding(
    level: EnumFloorFillLinesLevel, loop: str, why: str = "", fix: str = ""
) -> ModelFloorFillLinesFinding:
    return ModelFloorFillLinesFinding(level=level, loop=loop, why=why, fix=fix)


def _unread(request: ModelFloorFillLinesRequest) -> list[ModelFloorFillLinesFinding]:
    """One UNKNOWN per unread source, in the order given, each source once."""
    seen: set[str] = set()
    found: list[ModelFloorFillLinesFinding] = []
    for item in request.unread:
        source, _, _cause = item.partition(":")
        if source in seen:
            continue
        seen.add(source)
        found.append(
            _finding(
                _L.UNKNOWN,
                f"{source}-unread:{_cause}",
                fix=_UNREAD_FIX[EnumFloorFillLinesSource(source)],
            )
        )
    return found


def _results_of(
    line: ModelFloorFillLinesLine, request: ModelFloorFillLinesRequest
) -> list[ModelFloorFillLinesStartResult]:
    return [r for r in request.start_results if line_of_key(r.key) == line.line_id]


def _startable(
    line: ModelFloorFillLinesLine, request: ModelFloorFillLinesRequest
) -> ModelFloorFillLinesFinding:
    results = _results_of(line, request)
    started = [r for r in results if r.kind is _K.STARTED]
    if started:
        lanes = ",".join(str(r.lane) for r in started)
        return _finding(_L.NOTE, f"line-filled:{line.line_id}", f"lane={lanes}")
    if request.hold != "none":
        return _finding(_L.NOTE, f"line-held:{line.line_id}", request.hold)
    if request.free_slots_after == 0:
        return _finding(_L.NOTE, f"line-held:{line.line_id}", "no-free-slots")
    why = (
        f"next={line.next_task_id} ticket={line.ticket or 'none'} "
        f"slots_free={request.free_slots_after}"
    )
    failed = [r for r in results if r.kind is not _K.STARTED]
    if failed:
        own = [r for r in failed if r.key.endswith(f":{line.next_task_id}")]
        tried = (own or failed)[0]
        label = "start-failed" if tried.kind is _K.FAILED else tried.kind.value
        why += f" cause={label}:{tried.cause}"
    return _finding(_L.MISSING, f"line-stopped:{line.line_id}", why, _STOPPED_FIX)


def _line(
    line: ModelFloorFillLinesLine, request: ModelFloorFillLinesRequest
) -> list[ModelFloorFillLinesFinding]:
    if line.verdict in (_V.STARTABLE, _V.NEEDS_TICKET):
        return [_startable(line, request)]
    if line.verdict is _V.UNTICKETABLE:
        return [
            _finding(
                _L.MISSING,
                f"line-ticket:{line.line_id}",
                f"task={task.task_id} cause={task.cause}",
                _TICKET_FIX,
            )
            for task in line.untickable
        ]
    if line.verdict is _V.AT_CAPACITY:
        # A line this pass filled to its capacity reads AT_CAPACITY on the re-read; it is
        # still a filled line, so it prints what the pass did, not that it is full.
        started = [r for r in _results_of(line, request) if r.kind is _K.STARTED]
        if started:
            lanes = ",".join(str(r.lane) for r in started)
            return [_finding(_L.NOTE, f"line-filled:{line.line_id}", f"lane={lanes}")]
        return [_finding(_L.NOTE, f"line-at-capacity:{line.line_id}")]
    return [_finding(_L.NOTE, f"line-blocked:{line.line_id}", str(line.cause))]


def _reserved(
    entry: ModelFloorFillLinesEntry, request: ModelFloorFillLinesRequest
) -> ModelFloorFillLinesFinding | None:
    """An unresolved RESERVED entry past its timeout; before the timeout it is only waiting."""
    if (
        entry.key not in request.unresolved_keys
        or entry.state is not EnumFloorFillLinesEntryState.RESERVED
        or entry.reserved_at is None
    ):
        return None
    age = request.now - entry.reserved_at
    if age.total_seconds() < request.reserved_timeout_seconds:
        return None
    minutes = int(age.total_seconds() // 60)
    return _finding(
        _L.MISSING,
        f"line-reserved:{entry.key}",
        f"line={entry.line_id} age={minutes}m",
        _RESERVED_FIX,
    )


class HandlerFloorFillLines:
    """``handle(request) -> result``: the pass's findings, in the order they print."""

    def handle(self, request: ModelFloorFillLinesRequest) -> ModelFloorFillLinesResult:
        if request.unread:
            return ModelFloorFillLinesResult(findings=tuple(_unread(request)))
        found: list[ModelFloorFillLinesFinding] = []
        for line in request.lines:
            found.extend(_line(line, request))
        for entry in request.entries:
            reserved = _reserved(entry, request)
            if reserved is not None:
                found.append(reserved)
        return ModelFloorFillLinesResult(findings=tuple(found))


__all__: list[str] = ["HandlerFloorFillLines"]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deliver a host thermal guard's queued rows and post one alarm per hold episode (OMN-19998).

The guard (a host script on h202) writes a latch file when it closes the lab-run folder and a status row for every
transition, but its rows could not reach the ledger and piled up in an outbox file. One run of this handler:

* reads the latch and the last reading (a read that fails or whose shape changed is a typed error, and never reads as
  "the latch is gone": a malformed latch cannot post a release);
* posts ONE ledger MSG when the latch appears and ONE when it is gone after a trip, each carrying a request id derived
  from the episode so a retry is the same request and the ledger deduplicates it; the episode is advanced only after the
  ledger accepted the row;
* delivers the outbox rows: the outbox is claimed (moved aside) first, so a row the guard appends meanwhile goes to a new
  file and is never overwritten; delivered rows are gone, rows that could not be delivered go back, a row the ledger
  refuses goes to the refused file with its refusal text in the result. Nothing is dropped.

Every value (lane names, texts, file names, bounds) is the overlay's; the ports and the clock are injected.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..models import (
    EnumGuardReadState,
    EnumHoldReportError,
    ModelGuardRead,
    ModelHostThermalHoldReportRequest,
    ModelHostThermalHoldReportResult,
)
from ..protocols import (
    ProtocolEpisodeStore,
    ProtocolGuardState,
    ProtocolLedgerAppender,
)

#: The namespace the alarm request ids are derived under; fixed so a retry of one episode's alarm is one request.
ALARM_NAMESPACE = uuid.UUID("3f6d0a52-9c1e-5b7a-8d24-6e0f1c9a7b13")
TRIP, RELEASE = "trip", "release"
ACCEPTED = ("accepted", "duplicate")
_REQ_CELL = re.compile(r"^req=([0-9a-fA-F-]{36})$")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def row_with_req_last(row: str) -> tuple[str, str] | None:
    """The row with its ``req=<uuid>`` cell moved to the end (the bus append wants it last) and that request id.

    None when the row has no whole ``req=`` cell holding a uuid: such a row cannot be sent as the request it names."""
    cells = row.rstrip("\r\n").split(" | ")
    found = [
        (i, match.group(1))
        for i, c in enumerate(cells)
        if (match := _REQ_CELL.match(c.strip())) is not None
    ]
    if len(found) != 1:
        return None
    index, request_id = found[0]
    try:
        uuid.UUID(request_id)
    except ValueError:
        return None
    rest = [c for i, c in enumerate(cells) if i != index]
    return " | ".join([*rest, f"req={request_id}"]), request_id


def decide_alarm(latch_present: bool, episode_open: bool) -> str | None:
    """trip when the latch is there and no episode is open; release when it is gone and one is open; else nothing."""
    if latch_present and not episode_open:
        return TRIP
    if not latch_present and episode_open:
        return RELEASE
    return None


def _one_line(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _number(data: dict[str, Any] | None, key: str, fallback: float = 0.0) -> float:
    value = (data or {}).get(key)
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else fallback
    )


class HandlerHostThermalHoldReport:
    """One run: the alarm decision and the outbox delivery. The ports and the clock are injectable."""

    def __init__(
        self,
        guard: ProtocolGuardState,
        episodes: ProtocolEpisodeStore,
        appender: ProtocolLedgerAppender,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._guard = guard
        self._episodes = episodes
        self._appender = appender
        self._clock = clock

    def handle(
        self, request: ModelHostThermalHoldReportRequest
    ) -> ModelHostThermalHoldReportResult:
        errors: list[tuple[str, str]] = []
        latch = self._guard.read_latch()
        last = self._guard.read_last()
        episode = self._episodes.load()
        if latch.state in (
            EnumGuardReadState.UNREADABLE,
            EnumGuardReadState.SHAPE_CHANGED,
        ):
            code = (
                EnumHoldReportError.SHAPE_CHANGED
                if latch.state is EnumGuardReadState.SHAPE_CHANGED
                else EnumHoldReportError.LATCH_UNREADABLE
            )
            errors.append((code.value, latch.detail))
        if last.state is not EnumGuardReadState.PRESENT:
            errors.append(
                (
                    EnumHoldReportError.LAST_UNREADABLE.value,
                    f"{last.state.value}: {last.detail}",
                )
            )
        if episode is None:
            errors.append(
                (
                    EnumHoldReportError.EPISODE_UNREADABLE.value,
                    "the episode file cannot be read",
                )
            )

        alarm, status, request_id, alarm_row = "none", "", "", ""
        if episode is not None and latch.state in (
            EnumGuardReadState.ABSENT,
            EnumGuardReadState.PRESENT,
        ):
            kind = decide_alarm(
                latch.state is EnumGuardReadState.PRESENT, bool(episode.get("open"))
            )
            if kind is not None:
                alarm, status, request_id, alarm_row = self._alarm(
                    request, kind, latch, last, episode, errors
                )

        delivered, kept, deferred, refused = self._deliver(request, errors)
        alarm_done = alarm == "none" or status in (*ACCEPTED, "dry-run")
        ok = not errors and kept == 0 and not refused and alarm_done
        return ModelHostThermalHoldReportResult(
            host=request.host,
            alarm=alarm,
            alarm_status=status,
            alarm_request_id=request_id,
            alarm_row=alarm_row,
            delivered=delivered,
            kept=kept,
            deferred=deferred,
            refused=tuple(refused),
            errors=tuple(errors),
            dry_run=request.dry_run,
            ok=ok,
        )

    def _alarm(
        self,
        request: ModelHostThermalHoldReportRequest,
        kind: str,
        latch: ModelGuardRead,
        last: ModelGuardRead,
        episode: dict[str, Any],
        errors: list[tuple[str, str]],
    ) -> tuple[str, str, str, str]:
        now = self._clock()
        key = (
            str(latch.data["tripped_at"])
            if kind == TRIP and latch.data
            else str(episode.get("trip_key", ""))
        )
        pending = episode.get("pending") or {}
        request_id = (
            str(pending["request_id"])
            if pending.get("kind") == kind and pending.get("request_id")
            else str(uuid.uuid5(ALARM_NAMESPACE, f"{request.host}|{kind}|{key}"))
        )
        hottest = (
            _number(latch.data, "hottest_c")
            if kind == TRIP
            else _number(last.data, "hottest_c", _number(episode, "hottest_c"))
        )
        row = self._alarm_row(request, kind, now, request_id, hottest, key, latch.data)
        if request.dry_run:
            return kind, "dry-run", request_id, row
        outcome = self._appender.append(row, request_id)
        record = dict(episode)
        if outcome.status in ACCEPTED:
            record.pop("pending", None)
            record["open"] = kind == TRIP
            if kind == TRIP:
                record.update(
                    trip_key=key, hottest_c=hottest, opened_at=now.strftime(_STAMP)
                )
        else:
            record["pending"] = {"kind": kind, "request_id": request_id}
        try:
            self._episodes.save(record)
        except OSError as exc:
            errors.append(
                (
                    EnumHoldReportError.EPISODE_UNWRITABLE.value,
                    _one_line(str(exc))[:200],
                )
            )
        return kind, outcome.status, request_id, row

    def _alarm_row(
        self,
        request: ModelHostThermalHoldReportRequest,
        kind: str,
        now: datetime,
        request_id: str,
        hottest: float,
        key: str,
        latch: dict[str, Any] | None,
    ) -> str:
        overlay = request.overlay
        host_row = overlay.host_row(request.host)
        stamp = now.strftime(_STAMP)
        text = (overlay.trip_text if kind == TRIP else overlay.release_text).format(
            host=request.host,
            hottest_c=hottest,
            cpu_c=_number(latch, "cpu_c", hottest),
            gpu_c=_one_line(str((latch or {}).get("gpu_c", []))),
            tripped_at=key,
            released_at=stamp,
            latch_path=f"~/{host_row.guard_state_dir}/{overlay.latch_file}",
            operator_next=overlay.operator_next,
        )
        state = "tripped" if kind == TRIP else "released"
        return (
            f"{stamp} | MSG | from={host_row.hold_lane} | id={stamp}-{host_row.hold_lane} | to={overlay.to_lane}"
            f" | ticket={overlay.ticket} | host={request.host} | kind=thermal-hold | state={state}"
            f" | hottest={hottest:.1f}C | req={request_id} | {_one_line(text)}"
        )

    def _deliver(
        self,
        request: ModelHostThermalHoldReportRequest,
        errors: list[tuple[str, str]],
    ) -> tuple[int, int, int, list[tuple[str, str]]]:
        """(delivered, kept, deferred, refused) of one pass over the outbox."""
        if request.dry_run:
            rows = self._guard.peek_outbox()
            if rows is None:
                errors.append(
                    (
                        EnumHoldReportError.OUTBOX_UNREADABLE.value,
                        "the outbox cannot be read",
                    )
                )
                return 0, 0, 0, []
            return 0, 0, len(rows), []
        rows = self._guard.claim_outbox()
        if rows is None:
            errors.append(
                (
                    EnumHoldReportError.OUTBOX_UNREADABLE.value,
                    "the outbox cannot be read",
                )
            )
            return 0, 0, 0, []
        batch, later = (
            rows[: request.overlay.max_outbox_rows_per_run],
            rows[request.overlay.max_outbox_rows_per_run :],
        )
        delivered = 0
        back: list[str] = []
        refused: list[tuple[str, str]] = []
        refused_rows: list[tuple[str, str]] = []
        for row in batch:
            moved = row_with_req_last(row)
            if moved is None:
                refused.append(("", "no-request-id"))
                refused_rows.append((row, "no-request-id"))
                continue
            sendable, request_id = moved
            outcome = self._appender.append(sendable, request_id)
            if outcome.status in ACCEPTED:
                delivered += 1
            elif outcome.status == "refused":
                refused.append((request_id, outcome.message))
                refused_rows.append((row, outcome.message))
            else:
                back.append(row)
        try:
            self._guard.settle_outbox([*back, *later], refused_rows)
        except OSError as exc:
            errors.append(
                (EnumHoldReportError.SETTLE_FAILED.value, _one_line(str(exc))[:200])
            )
        return delivered, len(back), len(later), refused

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold a hot host and release it when it has cooled; write a ledger row for each (OMN-24562).

One run reads the host's temperature once and decides with hysteresis: a reading above ``hold_above_c`` holds the host; a held
host is released only by a reading below the lower ``release_below_c`` once it has been held ``min_hold_s``. A reading between the
two changes nothing, so a host oscillating around the hold threshold is held once and stays held.

The hold is the guard's declared hold file (written first, so the host is out of placement even when the ledger cannot be
reached); no folder's mode is ever changed. Each hold and release queues one ledger row in the episode file under a request id
derived from the hold, and a row leaves the queue only when the append command reports it accepted: a row that was not delivered
stays queued and the run is not ok. A host that cannot be read, a hold file that cannot be read and an episode file that cannot be
read are typed errors that change nothing and never read as a release.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from ..models import (
    EnumGuardAction,
    EnumGuardError,
    ModelHoldRecord,
    ModelHostThermalGuardRequest,
    ModelHostThermalGuardResult,
    ModelThermalReading,
)
from ..protocols import (
    ProtocolEpisodeStore,
    ProtocolHoldStore,
    ProtocolLaneCutter,
    ProtocolLedgerAppender,
    ProtocolThermalSensors,
)

#: The namespace the row request ids are derived under; fixed so a retry of one hold's or release's row is one request.
ROW_NAMESPACE = uuid.UUID("7b1d4c60-2e9a-5f38-a4d1-0c3e8b6f2a95")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
ACCEPTED = ("accepted", "duplicate")


def decide_action(
    held: bool,
    since: datetime | None,
    hottest_c: float,
    now: datetime,
    hold_above_c: float,
    release_below_c: float,
    min_hold_s: int,
) -> EnumGuardAction:
    """hold when not held and above the hold threshold; release when held, below the release threshold and held long enough."""
    if not held:
        return (
            EnumGuardAction.HOLD if hottest_c > hold_above_c else EnumGuardAction.NONE
        )
    if (
        hottest_c < release_below_c
        and since is not None
        and (now - since).total_seconds() >= min_hold_s
    ):
        return EnumGuardAction.RELEASE
    return EnumGuardAction.NONE


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_STAMP)


def _parse(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, _STAMP).replace(tzinfo=UTC)
    except ValueError:
        return None


def _one_line(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


class HandlerHostThermalGuard:
    """One run of the guard. The ports and the clock are injectable."""

    def __init__(
        self,
        sensors: ProtocolThermalSensors,
        holds: ProtocolHoldStore,
        episodes: ProtocolEpisodeStore,
        appender: ProtocolLedgerAppender,
        cutter: ProtocolLaneCutter,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sensors = sensors
        self._holds = holds
        self._episodes = episodes
        self._appender = appender
        self._cutter = cutter
        self._clock = clock

    def _row(
        self,
        kind: str,
        overlay: Any,
        host: str,
        lane: str,
        now: datetime,
        reading: ModelThermalReading,
        record: ModelHoldRecord,
        text: str,
    ) -> str:
        cells = [
            _stamp(now),
            kind,
            f"lane={lane}",
        ]
        if kind == "HOLD":
            cells += [
                f"id={record.hold_id}",
                f"surface=thermal-{host}",
                f"until={_stamp(now + timedelta(seconds=overlay.hold_row_until_s))}",
            ]
        elif kind == "RELEASE":
            cells += [
                f"re={record.hold_id}",
                f"surface=thermal-{host}",
                "result=PASS",
                "restored=yes",
            ]
        cells += [
            f"ticket={overlay.ticket}",
            f"actor={overlay.actor}",
            f"host={host}",
            f"hottest_c={_number(reading.hottest_c)}",
            f"hold_above_c={overlay.hold_above_c:g}",
            f"release_below_c={overlay.release_below_c:g}",
            f"min_hold_s={overlay.min_hold_s}",
            _one_line(text),
        ]
        return " | ".join(cells)

    def _text(
        self,
        template: str,
        overlay: Any,
        host: str,
        reading: ModelThermalReading,
        since: str,
        cut: tuple[str, ...] = (),
    ) -> str:
        return template.format(
            host=host,
            hottest_c=reading.hottest_c or 0.0,
            cpu_c=_number(reading.cpu_c),
            gpu_c=",".join(f"{g:.1f}" for g in reading.gpu_c) or "none",
            hold_above_c=overlay.hold_above_c,
            release_below_c=overlay.release_below_c,
            stop_above_c=overlay.stop_above_c or 0.0,
            min_hold_s=overlay.min_hold_s,
            since=since,
            cut=",".join(cut) or "none",
        )

    def handle(
        self, request: ModelHostThermalGuardRequest
    ) -> ModelHostThermalGuardResult:
        overlay = request.overlay
        host = request.host
        lane = overlay.host_row(host).lane
        now = self._clock()
        errors: list[tuple[str, str]] = []
        rows: list[str] = []
        cut: tuple[str, ...] = ()

        def result(
            reading: ModelThermalReading | None,
            action: EnumGuardAction,
            record: ModelHoldRecord | None,
            delivered: int = 0,
            pending: int = 0,
            refused: tuple[tuple[str, str], ...] = (),
        ) -> ModelHostThermalGuardResult:
            return ModelHostThermalGuardResult(
                host=host,
                hottest_c=None if reading is None else reading.hottest_c,
                action=action.value,
                held=record is not None,
                hold_id="" if record is None else record.hold_id,
                rows=tuple(rows),
                delivered=delivered,
                pending=pending,
                refused=refused,
                cut=cut,
                errors=tuple(errors),
                dry_run=request.dry_run,
                ok=not errors and pending == 0 and not refused,
            )

        reading = self._sensors.read()
        readable, record, detail = self._holds.read()
        episode = self._episodes.load()
        if not readable:
            errors.append((EnumGuardError.HOLD_UNREADABLE.value, detail))
            return result(reading, EnumGuardAction.NONE, record)
        if episode is None:
            errors.append(
                (
                    EnumGuardError.EPISODE_UNREADABLE.value,
                    "the episode file cannot be read",
                )
            )
            return result(reading, EnumGuardAction.NONE, record)
        hottest = reading.hottest_c
        if hottest is None:
            # No reading is not a safe reading: say so and change nothing.
            errors.append(
                (
                    EnumGuardError.NO_SENSOR_READING.value,
                    "neither the CPU nor a GPU could be read",
                )
            )
            return result(reading, EnumGuardAction.NONE, record)

        queue: list[dict[str, str]] = list(episode.get("pending", []))
        action = decide_action(
            record is not None,
            None if record is None else _parse(record.since),
            hottest,
            now,
            overlay.hold_above_c,
            overlay.release_below_c,
            overlay.min_hold_s,
        )
        current = record
        if action is EnumGuardAction.HOLD:
            since = _stamp(now)
            current = ModelHoldRecord(
                host,
                since,
                f"{since}-{lane}",
                hottest,
                overlay.hold_above_c,
                overlay.release_below_c,
            )
            row = self._row(
                "HOLD",
                overlay,
                host,
                lane,
                now,
                reading,
                current,
                self._text(overlay.hold_text, overlay, host, reading, since),
            )
            rows.append(row)
            queue.append(
                {
                    "row": row,
                    "request_id": str(
                        uuid.uuid5(ROW_NAMESPACE, f"{host}|hold|{since}")
                    ),
                }
            )
        elif action is EnumGuardAction.RELEASE and record is not None:
            row = self._row(
                "RELEASE",
                overlay,
                host,
                lane,
                now,
                reading,
                record,
                self._text(overlay.release_text, overlay, host, reading, record.since),
            )
            rows.append(row)
            queue.append(
                {
                    "row": row,
                    "request_id": str(
                        uuid.uuid5(ROW_NAMESPACE, f"{host}|release|{record.hold_id}")
                    ),
                }
            )
            current = None

        stopping = overlay.stop_above_c is not None and hottest > overlay.stop_above_c
        stop_due = stopping and (
            now.timestamp() - float(episode.get("stop_at", 0.0))
            >= overlay.stop_report_every_s
        )
        stop_at = float(episode.get("stop_at", 0.0))
        if stop_due and not request.dry_run:
            try:
                cut = self._cutter.cut()
            except OSError as exc:
                errors.append(
                    (EnumGuardError.LANES_NOT_CUT.value, f"{type(exc).__name__}")
                )
            else:
                stop_at = now.timestamp()
                row = self._row(
                    "STATUS",
                    overlay,
                    host,
                    lane,
                    now,
                    reading,
                    current or ModelHoldRecord(host, "", "", hottest, 0, 0),
                    self._text(
                        overlay.stop_text,
                        overlay,
                        host,
                        reading,
                        "" if current is None else current.since,
                        cut,
                    ),
                )
                rows.append(row)
                queue.append(
                    {
                        "row": row,
                        "request_id": str(
                            uuid.uuid5(ROW_NAMESPACE, f"{host}|stop|{_stamp(now)}")
                        ),
                    }
                )

        if request.dry_run:
            return result(reading, action, current, pending=len(queue) - len(rows))

        try:
            if action is EnumGuardAction.HOLD and current is not None:
                self._holds.write(current)
            elif action is EnumGuardAction.RELEASE:
                self._holds.clear()
        except OSError as exc:
            errors.append((EnumGuardError.HOLD_UNWRITABLE.value, type(exc).__name__))
            # The hold did not change on the host: queue nothing that says it did.
            return result(reading, EnumGuardAction.NONE, record)

        kept: list[dict[str, str]] = []
        refused: list[tuple[str, str]] = []
        delivered = 0
        for entry in queue:
            outcome = self._appender.append(entry["row"], entry["request_id"])
            if outcome.status in ACCEPTED:
                delivered += 1
            elif outcome.status == "refused":
                refused.append((entry["row"], outcome.message))
                errors.append((EnumGuardError.ROW_REFUSED.value, outcome.message))
            else:
                kept.append(entry)
                errors.append(
                    (
                        EnumGuardError.ROW_NOT_DELIVERED.value,
                        f"{outcome.status}: {outcome.message}",
                    )
                )
        try:
            self._episodes.save(
                {
                    "pending": kept,
                    "stop_at": stop_at,
                    "refused": [
                        *episode.get("refused", []),
                        *({"row": r, "reason": why} for r, why in refused),
                    ],
                }
            )
        except OSError as exc:
            errors.append((EnumGuardError.EPISODE_UNWRITABLE.value, type(exc).__name__))
        return result(
            reading,
            action,
            current,
            delivered=delivered,
            pending=len(kept),
            refused=tuple(refused),
        )

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ruling register read: ``handle(ModelRulingReadRequest) -> ModelRulingReadResult`` (OMN-24478).

Reads rows of the standing-rulings register concurrently through the port (the rows that run the
ledger reader one after another, because the query service they share answers one window at a time, holding a
cross-process lock so a second process's chain reads UNREAD instead of overlapping),
returning readings and emitting events in row order: ``answered`` (golden) for an OK reading,
``refused`` (error) for UNREAD.
The port never raises and a refused read is never an answer; judging the readings into
verdicts belongs to ``node_ruling_register_compute``. A journal write that fails costs
its row's event only: the remaining rows still read, and the failed rows come back in
``sink_errors``. The topics are a constructor parameter of two strings -- the CLI passes
them from the contract -- so no topic string is in this file.
"""

from __future__ import annotations

import fcntl
import tempfile
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.model_ruling_register import (
    EnumReadStatus,
    ModelRulingReading,
    ModelRulingReadRequest,
    ModelRulingReadResult,
    ModelRulingRow,
)
from omnimarket.nodes.node_ruling_register_effect.models import (
    ModelRulingReadEvent,
)
from omnimarket.nodes.node_ruling_register_effect.protocols import (
    ProtocolRulingEventSink,
    ProtocolRulingReadPort,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
MAX_CONCURRENT_READS = 8
# A row whose argv runs the ledger reader reads the same work-ledger query service as every other such row, and
# that service answers one window at a time: measured on 2026-11-17 (one 24 h window read in 69 s, eight at once
# all unanswered at 115 s). Those rows therefore run one after another, in row order, beside the concurrent rest.
LEDGER_READER_MARKER = "node_work_ledger_reader_effect"
# Serialization inside one process cannot keep a second process (the hourly unit's tick and a hand run, say) off the same
# query service, so the chain also holds an exclusive lock on this file for as long as it reads. The lock is taken without
# waiting: a chain that finds it held reads its rows UNREAD (a refused read, never TRUE) and the next tick reads them.
# The lock's default is derived from ONEX_STATE_DIR, which the unit and every lane on a host carry (on h201 the unit's
# {registry_root}/.onex_state and a lane's $OMNI_HOME/.onex_state are one directory), so a hand run and the unit's tick
# take the same file and exclude each other with no lock variable of their own. ONEX_RULING_REGISTER_LEDGER_LOCK names
# another file outright; with neither set the temp directory is the last resort.
LEDGER_LOCK_ENV = "ONEX_RULING_REGISTER_LEDGER_LOCK"
STATE_DIR_ENV = "ONEX_STATE_DIR"
LEDGER_LOCK_UNDER_STATE_DIR = Path("ruling-register") / "ledger-reader.lock"
DEFAULT_LEDGER_LOCK = (
    Path(tempfile.gettempdir()) / "onex-ruling-register-ledger-reader.lock"
)


def resolve_ledger_lock(environ: Mapping[str, str]) -> Path:
    """The ledger-reader lock file ``environ`` names: the lock variable, else under ONEX_STATE_DIR, else the temp file."""
    named = environ.get(LEDGER_LOCK_ENV, "").strip()
    if named:
        return Path(named)
    state_dir = environ.get(STATE_DIR_ENV, "").strip()
    if state_dir:
        return Path(state_dir) / LEDGER_LOCK_UNDER_STATE_DIR
    return DEFAULT_LEDGER_LOCK


def is_ledger_reader_row(row: ModelRulingRow) -> bool:
    """True when the row's argv (any token, so a ``sh -c`` script counts) runs the work-ledger reader."""
    return any(LEDGER_READER_MARKER in token for token in row.read.argv)


def worst_case_seconds(
    rows: Sequence[ModelRulingRow],
    timeout_seconds: int,
    cap: int = MAX_CONCURRENT_READS,
) -> int:
    """The longest a register read can take when every row waits the full route timeout.

    The ledger-reader rows form one task of ``count * timeout_seconds``; every other row is a task of
    ``timeout_seconds``; ``cap`` workers take the tasks in the handler's order (the chain first). The unit's run
    limit must exceed this.
    """
    chain = sum(1 for row in rows if is_ledger_reader_row(row))
    durations = ([chain * timeout_seconds] if chain else []) + [timeout_seconds] * (
        len(rows) - chain
    )
    free_at = [0] * max(1, min(len(durations), cap))
    for duration in durations:
        free_at.sort()
        free_at[0] += duration
    return max(free_at) if durations else 0


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerRulingRegisterRead:
    """``handle(ModelRulingReadRequest) -> ModelRulingReadResult``."""

    def __init__(
        self,
        port: ProtocolRulingReadPort,
        sink: ProtocolRulingEventSink,
        topics: Mapping[str, str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        ledger_lock: Path | None = None,
    ) -> None:
        self._port = port
        self._sink = sink
        self._topics = dict(topics)
        self._clock = clock
        self._ledger_lock = ledger_lock

    def handle(self, request: ModelRulingReadRequest) -> ModelRulingReadResult:
        readings = []
        sink_errors = []
        rows = request.policy.rows
        # One task per row, except that the ledger-reader rows form one task that reads them in row order.
        chain = [i for i, row in enumerate(rows) if is_ledger_reader_row(row)]
        tasks = ([chain] if chain else []) + [
            [i] for i, row in enumerate(rows) if not is_ledger_reader_row(row)
        ]

        def read_task(indexes: list[int]) -> list[tuple[int, ModelRulingReading]]:
            if self._ledger_lock is None or not is_ledger_reader_row(rows[indexes[0]]):
                return [(i, self._port.read(rows[i])) for i in indexes]
            return self._read_chain(rows, indexes)

        by_index: dict[int, ModelRulingReading] = {}
        with ThreadPoolExecutor(
            max_workers=max(1, min(len(tasks), MAX_CONCURRENT_READS))
        ) as executor:
            for task_readings in executor.map(read_task, tasks):
                by_index.update(task_readings)
        for i, row in enumerate(rows):
            reading = by_index[i]
            readings.append(reading)
            try:
                self._emit(request, row, reading)
            except OSError as exc:
                # A journal write that fails loses its row's event, not the
                # remaining rows' reads; the failures come back by row.
                sink_errors.append(f"{row.row_id}: {type(exc).__name__}")
        return ModelRulingReadResult(
            correlation_id=request.correlation_id,
            readings=tuple(readings),
            sink_errors=tuple(sink_errors),
        )

    def _read_chain(
        self, rows: Sequence[ModelRulingRow], indexes: list[int]
    ) -> list[tuple[int, ModelRulingReading]]:
        """The ledger-reader rows in row order while this process holds the cross-process lock."""
        assert self._ledger_lock is not None
        try:
            self._ledger_lock.parent.mkdir(parents=True, exist_ok=True)
            handle = self._ledger_lock.open("a+")
        except OSError as exc:
            return self._unread_chain(
                rows, indexes, f"ledger lock unopened: {type(exc).__name__}"
            )
        with handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return self._unread_chain(
                    rows, indexes, "ledger lock held by another process"
                )
            try:
                return [(i, self._port.read(rows[i])) for i in indexes]
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _unread_chain(
        self, rows: Sequence[ModelRulingRow], indexes: list[int], detail: str
    ) -> list[tuple[int, ModelRulingReading]]:
        return [
            (
                i,
                ModelRulingReading(
                    row_id=rows[i].row_id,
                    status=EnumReadStatus.UNREAD,
                    read_at=self._clock(),
                    detail=detail,
                ),
            )
            for i in indexes
        ]

    def _emit(
        self,
        request: ModelRulingReadRequest,
        row: ModelRulingRow,
        reading: ModelRulingReading,
    ) -> None:
        answered = reading.status is EnumReadStatus.OK
        event = ModelRulingReadEvent(
            kind="answered" if answered else "refused",
            topic=self._topics[
                "ruling_read_answered" if answered else "ruling_read_refused"
            ],
            row_id=row.row_id,
            ruling=row.ruling,
            status=reading.status,
            exit_code=reading.exit_code,
            detail=reading.detail,
            read_at=reading.read_at,
            correlation_id=request.correlation_id,
        )
        self._sink.emit(event)


__all__ = [
    "DEFAULT_LEDGER_LOCK",
    "LEDGER_LOCK_ENV",
    "LEDGER_LOCK_UNDER_STATE_DIR",
    "LEDGER_READER_MARKER",
    "MAX_CONCURRENT_READS",
    "STATE_DIR_ENV",
    "HandlerRulingRegisterRead",
    "is_ledger_reader_row",
    "load_topics",
    "resolve_ledger_lock",
    "worst_case_seconds",
]

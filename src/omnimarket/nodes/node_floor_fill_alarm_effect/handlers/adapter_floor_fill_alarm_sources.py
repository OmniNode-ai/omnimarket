# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapters of the alarm check over what the fill leaves behind: its stored answer and state rows, the ledger file and
the pool read.

The fill stores one answer (its latest run, with the free slots it saw and the lanes it started, ``seq`` rising by one
per run) and its state (each key's lane and start). ``StoredAnswerRunSource`` keeps the run history the IDLE rule needs
in its own log, one line per stored answer ``seq``; a gap in ``seq`` is a run the check never saw and is refused, and the
runs before a gap are never joined to the runs after it. Both stored rows are read strictly: a row that lacks a field the
fill writes is refused, never read as no lanes and 0 sent. The ledger is read from its file and written only through the
declared append command (the grammar-checking ledger writer), never by hand.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from omnimarket.models.floor_fill import ModelFloorFillState
from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
    ModelFloorFillPoolRead,
    ModelFloorFillRun,
    ModelFloorFillSentLane,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.protocols.protocol_floor_fill_alarm_check import (
    AlarmSourceError,
)

ANSWER_ROW_SUFFIX = ":answer"
FLOOR_KEY_PREFIX = "floor:"
CELL_SEPARATOR = " | "
RUN_HISTORY_KEPT = 50


class _StoredAnswer(BaseModel):
    """The fill's stored answer as the check reads it: every field the check relies on is required."""

    model_config = ConfigDict(extra="ignore")

    lines: tuple[str, ...]
    observed_at: AwareDatetime
    seq: int
    # None is a run whose pool read failed; it is required so that an answer of the old shape is refused, not read as 0.
    free_slots: int | None
    lanes_started: int


def _cause(lines: Sequence[str]) -> str:
    return next((line for line in lines if line.startswith("UNKNOWN")), "")


class StoredAnswerRunSource:
    """The fill's runs: each new stored answer is added to the history log, and the log is the answer."""

    def __init__(
        self, load_payload: Callable[[str], str | None], scope: str, log_path: Path
    ) -> None:
        self._load = load_payload
        self._key = scope + ANSWER_ROW_SUFFIX
        self._log = log_path

    def _history(self) -> list[tuple[int, ModelFloorFillRun]]:
        if not self._log.exists():
            return []
        out: list[tuple[int, ModelFloorFillRun]] = []
        try:
            for line in self._log.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    item = json.loads(line)
                    out.append(
                        (
                            int(item["seq"]),
                            ModelFloorFillRun.model_validate(item["run"]),
                        )
                    )
        except (OSError, ValueError, KeyError, ValidationError) as exc:
            raise AlarmSourceError(f"run log {self._log} unreadable: {exc}") from exc
        return out

    def read_runs(self, now: datetime) -> tuple[ModelFloorFillRun, ...]:
        history = self._history()
        try:
            payload = self._load(self._key)
        except Exception as exc:
            raise AlarmSourceError(f"stored answer unreadable: {exc}") from exc
        if payload is None:
            return tuple(run for _, run in history[-RUN_HISTORY_KEPT:])
        try:
            stored = _StoredAnswer.model_validate_json(payload)
        except ValidationError as exc:
            raise AlarmSourceError(f"stored answer unparseable: {exc}") from exc
        if history and stored.seq <= history[-1][0]:
            return tuple(run for _, run in history[-RUN_HISTORY_KEPT:])
        run = ModelFloorFillRun(
            observed_at=stored.observed_at,
            free_slots=stored.free_slots,
            lanes_sent=stored.lanes_started,
            cause=_cause(stored.lines),
        )
        gap = bool(history) and stored.seq != history[-1][0] + 1
        if gap:
            missed = stored.seq - history[-1][0] - 1
            last = history[-1][0]
            # The runs before the gap are dropped: two runs on either side of a run the check never saw are not two
            # runs in a row. The log starts again at this run, so the refusal is made once and the next check reads on.
            history = [(stored.seq, run)]
            self._write(history)
            raise AlarmSourceError(
                f"run gap: stored answer seq {stored.seq} follows logged seq {last}; "
                f"{missed} run(s) were never read, so the runs before it are not joined to it"
            )
        history.append((stored.seq, run))
        self._write(history[-RUN_HISTORY_KEPT:])
        return tuple(run for _, run in history[-RUN_HISTORY_KEPT:])

    def _write(self, history: list[tuple[int, ModelFloorFillRun]]) -> None:
        text = "".join(
            json.dumps({"seq": seq, "run": json.loads(run.model_dump_json())}) + "\n"
            for seq, run in history
        )
        try:
            self._log.parent.mkdir(parents=True, exist_ok=True)
            self._log.write_text(text, encoding="utf-8")
        except OSError as exc:
            raise AlarmSourceError(f"run log {self._log} not written: {exc}") from exc


class FileLedgerReader:
    """The ledger rows from the rolling ledger file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def rows(self) -> tuple[str, ...]:
        try:
            return tuple(self._path.read_text(encoding="utf-8").splitlines())
        except OSError as exc:
            raise AlarmSourceError(f"ledger {self._path} unreadable: {exc}") from exc


class CommandLedger:
    """Read the ledger file; append a row only through the declared append command (``{row}`` stands for the row)."""

    def __init__(
        self, path: Path, append_argv: Sequence[str], timeout_seconds: int = 60
    ) -> None:
        if not any("{row}" in part for part in append_argv):
            raise ValueError("the ledger append command carries no {row} element")
        self._reader = FileLedgerReader(path)
        self._argv = tuple(append_argv)
        self._timeout = timeout_seconds

    def rows(self) -> tuple[str, ...]:
        return self._reader.rows()

    def append(self, row: str) -> None:
        argv = [part.replace("{row}", row) for part in self._argv]
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True, timeout=self._timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AlarmSourceError(f"ledger append did not run: {exc}") from exc
        if done.returncode != 0:
            raise AlarmSourceError(
                f"ledger append refused rc={done.returncode}: {done.stderr.strip()[-200:]}"
            )


def _row_cells(row: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for cell in row.split(CELL_SEPARATOR):
        name, sep, value = cell.strip().partition("=")
        if sep:
            cells.setdefault(name, value)
    return cells


def _claim_at(rows: Sequence[str], lane: str, key: str) -> datetime | None:
    for row in rows:
        parts = row.split(CELL_SEPARATOR)
        if len(parts) < 2 or parts[1].strip() != "CLAIM":
            continue
        cells = _row_cells(row)
        if cells.get("lane") == lane and cells.get("key") == key:
            try:
                return datetime.fromisoformat(parts[0].strip())
            except ValueError:
                return None
    return None


def _read_state(payload: str) -> ModelFloorFillState:
    """The fill's state row as the shared model reads it; a row with no ``entries`` is refused, never read as empty."""
    try:
        raw = json.loads(payload)
    except ValueError as exc:
        raise AlarmSourceError(f"fill state unparseable: {exc}") from exc
    if not isinstance(raw, dict) or "entries" not in raw:
        raise AlarmSourceError(
            "fill state refused: the row has no entries, so it cannot be read as no lanes started"
        )
    try:
        return ModelFloorFillState.model_validate(raw)
    except ValidationError as exc:
        raise AlarmSourceError(f"fill state unparseable: {exc}") from exc


def _claim_since(claim: datetime | None, started: datetime) -> datetime | None:
    """The CLAIM that belongs to this start, or None when the ledger holds none for it yet.

    Ledger stamps are whole seconds and the start's time is not, so the two are compared at second precision: a CLAIM
    stamped in the second of the start is that start's CLAIM, read as arriving at the start. Only a CLAIM stamped in an
    earlier second is an earlier start's.
    """
    if claim is None or claim < started.replace(microsecond=0):
        return None
    return max(claim, started)


class StoredStateSentLanes:
    """The lanes the fill started (its floor-keyed state entries) and the ledger's CLAIM time for each."""

    def __init__(
        self,
        load_payload: Callable[[str], str | None],
        scope: str,
        ledger_rows: Callable[[], tuple[str, ...]],
    ) -> None:
        self._load = load_payload
        self._scope = scope
        self._rows = ledger_rows

    def read_sent_lanes(self) -> tuple[ModelFloorFillSentLane, ...]:
        try:
            payload = self._load(self._scope)
        except Exception as exc:
            raise AlarmSourceError(f"fill state unreadable: {exc}") from exc
        if payload is None:
            return ()
        state = _read_state(payload)
        rows = self._rows()
        lanes: list[ModelFloorFillSentLane] = []
        for entry in state.entries.values():
            started = entry.sent_at or entry.reserved_at
            if entry.ended or started is None or entry.lane is None:
                continue
            claim = _claim_at(rows, entry.lane, entry.key)
            lanes.append(
                ModelFloorFillSentLane(
                    lane=entry.lane,
                    key=entry.key,
                    sent_at=started,
                    claim_at=_claim_since(claim, started),
                )
            )
        return tuple(lanes)


class CallablePoolReader:
    """One pool read: the free slots, or an unread answer naming why (the errors the pool raises for a failed read)."""

    def __init__(
        self,
        read_free_slots: Callable[[], int],
        unread_errors: tuple[type[Exception], ...],
    ) -> None:
        self._read = read_free_slots
        self._errors = unread_errors

    def read_pool(self) -> ModelFloorFillPoolRead:
        try:
            return ModelFloorFillPoolRead(free_slots=self._read())
        except self._errors as exc:
            return ModelFloorFillPoolRead(
                free_slots=None, unread_cause=f"pool-unread:{exc}"
            )


__all__: list[str] = [
    "CallablePoolReader",
    "CommandLedger",
    "FileLedgerReader",
    "StoredAnswerRunSource",
    "StoredStateSentLanes",
]

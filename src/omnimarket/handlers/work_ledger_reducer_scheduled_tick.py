# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tick of the scheduled work-ledger reducer: read a bounded window, run the guard replay and rebuild.

The scheduled job node reaches the work-ledger reducer through this shared module, so no node imports another node's
models package. The overlay names the environment variable holding the bus read command; an unbound read command
raises ``WorkLedgerReadError`` and the scheduled job node turns it into a failed run event.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple, Protocol

from pydantic import BaseModel, ConfigDict

from omnimarket.models.work_ledger_query.model_work_ledger_query import (
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    wire_correlation_id,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.handlers.handler_work_ledger_reducer import (
    HandlerWorkLedgerReducer,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    EnumJudgeVerdict,
    ModelGuardReplayRequest,
    ModelJudgeVerdict,
    ModelRecordedRow,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_rebuild import (
    EnumAcceptedRowSource,
    ModelAcceptedRowEvent,
    ModelRebuildRequest,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_reducer import (
    ModelWorkLedgerReducerRequest,
    ModelWorkLedgerReducerResult,
)

if TYPE_CHECKING:
    # The node package imports its handler, which imports this module: the overlay type is annotation-only here.
    from omnimarket.nodes.node_work_ledger_reducer_scheduled_effect.models import (
        ModelWorkLedgerReducerScheduleOverlay,
    )

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_READ_REFUSED = re.compile(r"Work-ledger read refused \(([A-Za-z_][A-Za-z0-9_]*)\)")


class WorkLedgerReadError(RuntimeError):
    """The bus read command is not bound in the runtime."""


class ModelWorkLedgerRowsRead(BaseModel):
    """The answer of one bounded ledger read: the rows, or the named error. An error is never an empty answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelWorkLedgerRowRecord, ...] = ()
    error: str | None = None


class ProtocolWorkLedgerRowsReader(Protocol):
    """Reads a bounded window of work-ledger rows."""

    def read_rows(
        self,
        *,
        since: datetime,
        until: datetime,
        kinds: tuple[str, ...] | None,
        limit: int,
        correlation_id: str,
    ) -> ModelWorkLedgerRowsRead: ...


class InMemoryWorkLedgerRowsReader:
    """The in-memory default profile: the held rows filtered by window and kinds; ``read_error`` injects a fault."""

    def __init__(self, rows: Sequence[ModelWorkLedgerRowRecord]) -> None:
        self._rows = tuple(rows)
        self.read_error: str | None = None

    def read_rows(
        self,
        *,
        since: datetime,
        until: datetime,
        kinds: tuple[str, ...] | None,
        limit: int,
        correlation_id: str,
    ) -> ModelWorkLedgerRowsRead:
        if self.read_error is not None:
            return ModelWorkLedgerRowsRead(error=self.read_error)
        matched = tuple(
            row
            for row in self._rows
            if since <= row.row_ts < until and (kinds is None or row.row_type in kinds)
        )
        if len(matched) > limit:
            return ModelWorkLedgerRowsRead(error="read-answer-truncated")
        return ModelWorkLedgerRowsRead(rows=matched)


class CommandWorkLedgerRowsReader:
    """Reads a bounded window of work-ledger rows through the bus read command."""

    def __init__(self, argv: Sequence[str], timeout_seconds: int) -> None:
        self._argv = list(argv)
        self._timeout_seconds = timeout_seconds

    def read_rows(
        self,
        *,
        since: datetime,
        until: datetime,
        kinds: tuple[str, ...] | None,
        limit: int,
        correlation_id: str,
    ) -> ModelWorkLedgerRowsRead:
        query: dict[str, object] = {
            "query": "rows",
            "since": since.strftime(_STAMP_FORMAT),
            "until": until.strftime(_STAMP_FORMAT),
            "limit": limit,
            "correlation_id": wire_correlation_id(correlation_id),
        }
        if kinds is not None:
            query["kinds"] = list(kinds)
        try:
            done = subprocess.run(
                self._argv,
                input=json.dumps(query),
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ModelWorkLedgerRowsRead(
                error=f"read-command-failed:{type(exc).__name__}"
            )
        if done.returncode != 0:
            refused = _READ_REFUSED.search(done.stderr)
            suffix = f":{refused.group(1)}" if refused else ""
            return ModelWorkLedgerRowsRead(
                error=f"read-command-exit-{done.returncode}{suffix}"
            )
        try:
            answer = json.loads(done.stdout)
        except json.JSONDecodeError:
            return ModelWorkLedgerRowsRead(error="read-answer-not-json")
        if not isinstance(answer, dict):
            return ModelWorkLedgerRowsRead(error="read-answer-not-json")
        if answer.get("status", "ok") != "ok" or answer.get("error"):
            return ModelWorkLedgerRowsRead(error="read-answer-error")
        raw_rows = answer.get("rows")
        if not isinstance(raw_rows, list):
            return ModelWorkLedgerRowsRead(error="read-answer-without-rows")
        matched = answer.get("matched")
        if answer.get("next_cursor") or (
            isinstance(matched, int) and matched > len(raw_rows)
        ):
            return ModelWorkLedgerRowsRead(error="read-answer-truncated")
        rows: list[ModelWorkLedgerRowRecord] = []
        for item in raw_rows:
            try:
                rows.append(ModelWorkLedgerRowRecord.model_validate(item))
            except ValueError:
                return ModelWorkLedgerRowsRead(error="read-answer-malformed-row")
        return ModelWorkLedgerRowsRead(rows=tuple(rows))


def build_rows_reader(
    overlay: ModelWorkLedgerReducerScheduleOverlay,
    environ: Mapping[str, str] | None = None,
) -> CommandWorkLedgerRowsReader:
    """The command reader of the variable the overlay names; an unset one refuses naming itself."""
    env = os.environ if environ is None else environ
    command = env.get(overlay.read_command_env, "").strip()
    if not command:
        raise WorkLedgerReadError(f"{overlay.read_command_env} is not set")
    return CommandWorkLedgerRowsReader(
        shlex.split(command), overlay.read_timeout_seconds
    )


class ModelWorkLedgerReducerTickOutcome(NamedTuple):
    """How one scheduled run went: failed, and the detail the run event carries."""

    failed: bool
    detail: str


def _order(row: ModelWorkLedgerRowRecord) -> tuple[datetime, datetime, str]:
    return (row.row_ts, row.projected_at or row.row_ts, row.row_id)


def _refused(operation: str, exc: ValueError) -> ModelWorkLedgerReducerTickOutcome:
    return ModelWorkLedgerReducerTickOutcome(
        True, f"{operation} refused: {type(exc).__name__}: {str(exc)[:200]}"
    )


def run_work_ledger_reducer_tick(
    *,
    overlay: ModelWorkLedgerReducerScheduleOverlay,
    reader: ProtocolWorkLedgerRowsReader,
    clock: Callable[[], datetime],
    reducer: Callable[[ModelWorkLedgerReducerRequest], ModelWorkLedgerReducerResult]
    | None = None,
    correlation_id: str | None = None,
) -> ModelWorkLedgerReducerTickOutcome:
    """Read the bounded window, run the reducer's guard replay and rebuild on it, and name any refusal."""
    run_id = correlation_id if correlation_id is not None else uuid.uuid4().hex
    reduce = reducer if reducer is not None else HandlerWorkLedgerReducer().handle
    until = clock()
    window_start = until - timedelta(seconds=overlay.window_seconds)
    window = reader.read_rows(
        since=window_start,
        until=until,
        kinds=None,
        limit=overlay.row_limit,
        correlation_id=run_id,
    )
    if window.error is not None:
        return ModelWorkLedgerReducerTickOutcome(
            True, f"window read failed: {window.error}"
        )
    prior = reader.read_rows(
        since=window_start - timedelta(seconds=overlay.prior_lookback_seconds),
        until=window_start,
        kinds=overlay.prior_kinds,
        limit=overlay.row_limit,
        correlation_id=run_id,
    )
    if prior.error is not None:
        return ModelWorkLedgerReducerTickOutcome(
            True, f"prior read failed: {prior.error}"
        )
    window_rows = sorted(window.rows, key=_order)
    prior_rows = sorted(prior.rows, key=_order)
    if not window_rows:
        return ModelWorkLedgerReducerTickOutcome(True, "window held no rows")
    try:
        replayed = reduce(
            ModelWorkLedgerReducerRequest(
                operation="guard_replay",
                replay=ModelGuardReplayRequest(
                    ledger_name=overlay.ledger_name,
                    prior_accepted=tuple(row.raw_row for row in prior_rows),
                    entries=tuple(
                        ModelRecordedRow(
                            row_ref=row.row_id,
                            row=row.raw_row,
                            recorded=ModelJudgeVerdict(verdict=EnumJudgeVerdict.ACCEPT),
                            at=row.row_ts,
                        )
                        for row in window_rows
                    ),
                ),
            )
        )
    except ValueError as exc:
        return _refused("guard_replay", exc)
    if replayed.replay is None:
        return ModelWorkLedgerReducerTickOutcome(
            True, "guard_replay answered no replay result"
        )
    if replayed.replay.differences:
        first = replayed.replay.differences[0]
        return ModelWorkLedgerReducerTickOutcome(
            True,
            f"guard replay differences={len(replayed.replay.differences)} "
            f"first={first.position}:{first.row_ref[:12]}:{first.difference[:120]}",
        )
    sequenced = [row for row in window_rows if row.ledger_seq is not None]
    if sequenced:
        try:
            rebuilt = reduce(
                ModelWorkLedgerReducerRequest(
                    operation="rebuild",
                    rebuild=ModelRebuildRequest(
                        events=tuple(
                            ModelAcceptedRowEvent(
                                ledger_id=overlay.ledger_id,
                                row_id=row.row_id,
                                raw_row=row.raw_row,
                                source=EnumAcceptedRowSource.LIVE,
                                ledger_seq=row.ledger_seq,
                            )
                            for row in sequenced
                        )
                    ),
                )
            )
        except ValueError as exc:
            return _refused("rebuild", exc)
        if rebuilt.rebuild is None:
            return ModelWorkLedgerReducerTickOutcome(
                True, "rebuild answered no rebuild result"
            )
        if rebuilt.rebuild.differences:
            return ModelWorkLedgerReducerTickOutcome(
                True, f"rebuild differences={len(rebuilt.rebuild.differences)}"
            )
    return ModelWorkLedgerReducerTickOutcome(
        False,
        f"window_rows={len(window_rows)} prior_rows={len(prior_rows)} "
        f"replayed={replayed.replay.judged} differences=0 "
        f"rebuild_events={len(sequenced)} rebuild={'ran' if sequenced else 'not-run'} "
        f"unsequenced={len(window_rows) - len(sequenced)}",
    )


__all__: list[str] = [
    "CommandWorkLedgerRowsReader",
    "InMemoryWorkLedgerRowsReader",
    "ModelWorkLedgerReducerTickOutcome",
    "ModelWorkLedgerRowsRead",
    "ProtocolWorkLedgerRowsReader",
    "WorkLedgerReadError",
    "build_rows_reader",
    "run_work_ledger_reducer_tick",
]

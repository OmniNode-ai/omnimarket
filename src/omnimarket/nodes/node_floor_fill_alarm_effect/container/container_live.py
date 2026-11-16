# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The binding site of the alarm check: the four ports are bound to what the fill leaves behind, and one check is one call.

Bound here: the fill's runs (its stored answer ``<scope>:answer``, kept as a run log), its started lanes (the state row
under ``<scope>`` with the CLAIM time of each from the ledger file), the pool (the lane host pool snapshot the fill
reads, read again by the check itself and refused when stale) and the ledger (read from its file, written only through the
declared append command). ``load_payload`` is the fill's row reader (``store_payload_loader`` over its store).

``run_alarm_check`` is the whole check as one run that returns what it found and logs one line for it; it starts no timer, unit or scheduler. The
schedule that calls it belongs to the Temporal workflow that runs the floor fill, not to this node.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.adapter_floor_fill_alarm_sources import (
    CallablePoolReader,
    CommandLedger,
    StoredAnswerRunSource,
    StoredStateSentLanes,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.handlers.handler_floor_fill_alarm_check import (
    HandlerFloorFillAlarmCheck,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.models import (
    ModelFloorFillAlarmCheckResult,
    ModelFloorFillAlarmSettings,
)
from omnimarket.nodes.node_floor_fill_alarm_effect.protocols.protocol_floor_fill_alarm_check import (
    AlarmSourceError,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    SnapshotFloorPool,
    StampedSnapshotFileLaneHostPool,
)


class _StateRead(Protocol):
    @property
    def payload(self) -> str | None: ...


class _StateStore(Protocol):
    def load(self, key: str) -> _StateRead: ...


def store_payload_loader(store: _StateStore) -> Callable[[str], str | None]:
    """The row reader of the check over the fill's store: a key's payload, or None when the row does not exist."""
    return lambda key: store.load(key).payload


def build_live_alarm_check(
    settings: ModelFloorFillAlarmSettings,
    *,
    load_payload: Callable[[str], str | None],
    pool_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> HandlerFloorFillAlarmCheck:
    """The handler with all four ports bound; ``pool_clock`` is the time the snapshot's age is taken against."""
    ledger = CommandLedger(Path(settings.ledger_path), settings.ledger_append_command)
    pool = SnapshotFloorPool(
        StampedSnapshotFileLaneHostPool(
            path=Path(settings.pool_snapshot_path),
            record_suffix=settings.pool_record_suffix,
            max_age_seconds=settings.pool_snapshot_max_age_seconds,
            clock=pool_clock,
        ),
        settings.pool_engine,
    )
    return HandlerFloorFillAlarmCheck(
        runs=StoredAnswerRunSource(
            load_payload, settings.scope, Path(settings.run_log_path)
        ),
        lanes=StoredStateSentLanes(load_payload, settings.scope, ledger.rows),
        pool=CallablePoolReader(pool.free_slots, (FloorFillUnreadableError,)),
        ledger=ledger,
    )


def _log_check(settings: ModelFloorFillAlarmSettings, line: dict[str, object]) -> None:
    path = Path(settings.check_log_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError as exc:
        raise AlarmSourceError(f"check log {path} not written: {exc}") from exc


def run_alarm_check(
    settings: ModelFloorFillAlarmSettings,
    *,
    load_payload: Callable[[str], str | None],
    now: datetime | None = None,
    pool_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ModelFloorFillAlarmCheckResult:
    """One check, end to end, logged as one line whether it found nothing, wrote rows or could not read a source.

    A source that cannot be read raises ``AlarmSourceError`` after its failure is logged, and no row is written.
    """
    at = now or datetime.now(UTC)
    handler = build_live_alarm_check(
        settings, load_payload=load_payload, pool_clock=pool_clock
    )
    try:
        result = handler.handle(settings.check_request(at))
    except AlarmSourceError as exc:
        _log_check(
            settings, {"at": at.isoformat(), "outcome": "failed", "error": str(exc)}
        )
        raise
    _log_check(
        settings,
        {
            "at": at.isoformat(),
            "outcome": "checked",
            "alarms_found": result.alarms_found,
            "rows_written": len(result.rows_written),
            "rows_suppressed": result.rows_suppressed,
            "runs_read": result.runs_read,
            "lanes_read": result.lanes_read,
            "pool_free_slots": result.pool_free_slots,
        },
    )
    return result


__all__: list[str] = [
    "build_live_alarm_check",
    "run_alarm_check",
    "store_payload_loader",
]

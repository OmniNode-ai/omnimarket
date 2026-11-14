# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hourly check's record step as an effect (OMN-23734).

Behaviour-preserving port of the rows the retired throughput_tick.py kept in its state directory:
``write_heartbeat`` (the heartbeat the checker reads), ``record_open_count`` (the open-count history the
trend finding reads), ``record_repo_status`` (the open status-check units and the verdict events) and
``write_goal_record`` (the goal check's record). Every write goes through ``ProtocolTickStateStore``;
a write that cannot be made is never raised: it is the error text the script returned, as a finding
the caller folds into the tick, because the next run would otherwise read a trend or a unit set with
this run missing and say nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from omnimarket.nodes.node_throughput_tick_record_effect.handlers.tick_record_ports import (
    FileTickStateStore,
)
from omnimarket.nodes.node_throughput_tick_record_effect.models.model_throughput_tick_record import (
    ModelRecordFinding,
    ModelThroughputTickRecordRequest,
    ModelThroughputTickRecordResult,
)
from omnimarket.nodes.node_throughput_tick_record_effect.protocols.protocol_tick_state_store import (
    ProtocolTickStateStore,
)

HEARTBEAT_FILE = "throughput-tick.heartbeat"
HISTORY_FILE = "throughput-tick.open-history.json"
HISTORY_KEEP = 12
REPO_STATUS_FILE = "throughput-tick.repo-status.json"
REPO_STATUS_EVENTS = "throughput-tick.repo-status-events.jsonl"
GOAL_STATE_FILE = "goal-check.json"
STATE_DIR_FIX = (
    "make the state directory writable (OMNI_SESSION_START_STATE_DIR names another)"
)


class HandlerThroughputTickRecord:
    """Keep the tick's rows; return what was kept and the findings for what could not be."""

    def __init__(self, store: ProtocolTickStateStore | None = None) -> None:
        self._store = store

    def handle(
        self, request: ModelThroughputTickRecordRequest
    ) -> ModelThroughputTickRecordResult:
        store = self._store or FileTickStateStore(Path(request.state_dir))
        written: list[str] = []
        findings: list[ModelRecordFinding] = []
        heartbeat_error: str | None = None
        if request.heartbeat is not None:
            beat = request.heartbeat
            heartbeat_error = self._attempt(
                store,
                HEARTBEAT_FILE,
                lambda: store.write_atomic(
                    HEARTBEAT_FILE, f"{beat.stamp} pid={beat.pid}\n"
                ),
                same_path=True,
            )
            if heartbeat_error is None:
                written.append(HEARTBEAT_FILE)
        if request.open_count is not None:
            row = request.open_count
            points = [
                {**p, "basis": row.basis}
                for p in [*row.history, {"at": row.at, "open": row.count}]
            ][-HISTORY_KEEP:]
            why = self._attempt(
                store,
                HISTORY_FILE,
                lambda: store.write_atomic(
                    HISTORY_FILE, json.dumps({"history": points}) + "\n"
                ),
                same_path=True,
            )
            if why is None:
                written.append(HISTORY_FILE)
            else:
                findings.append(
                    ModelRecordFinding(
                        loop="open-trend-history",
                        why=f"cannot write {why}",
                        fix=STATE_DIR_FIX,
                    )
                )
        if request.repo_status is not None:
            status = request.repo_status
            events = [v for v in status.verdicts if v.get("event")]
            units = [
                {
                    "repo": v["repo"],
                    "opened_at": v["opened_at"],
                    "verdict": v["verdict"],
                }
                for v in status.verdicts
            ]

            def keep_units() -> None:
                if events:
                    store.append_text(
                        REPO_STATUS_EVENTS,
                        "".join(
                            json.dumps(
                                {"kind": "repo-status-verdict", "at": status.at, **v}
                            )
                            + "\n"
                            for v in events
                        ),
                    )
                store.write_atomic(
                    REPO_STATUS_FILE, json.dumps({"open_units": units}) + "\n"
                )

            # the script names the units file whichever of the two writes failed
            why = self._attempt(store, REPO_STATUS_FILE, keep_units, same_path=True)
            if why is None:
                written.extend([REPO_STATUS_EVENTS] if events else [])
                written.append(REPO_STATUS_FILE)
            else:
                findings.append(
                    ModelRecordFinding(
                        loop="repo-status-units",
                        why=f"cannot write {why}",
                        fix=STATE_DIR_FIX,
                    )
                )
        if request.goal_record is not None:
            record = request.goal_record
            why = self._attempt(
                store,
                GOAL_STATE_FILE,
                lambda: store.write_atomic(
                    GOAL_STATE_FILE, json.dumps(record, sort_keys=True) + "\n"
                ),
                same_path=False,
            )
            if why is None:
                written.append(GOAL_STATE_FILE)
            else:
                findings.append(
                    ModelRecordFinding(
                        loop="goal-check-record",
                        why=f"cannot write {store.path(GOAL_STATE_FILE)}: {why}",
                        fix=STATE_DIR_FIX,
                    )
                )
        return ModelThroughputTickRecordResult(
            heartbeat_write_error=heartbeat_error,
            written=written,
            findings=findings,
            ok=not findings and heartbeat_error is None,
        )

    @staticmethod
    def _attempt(
        store: ProtocolTickStateStore,
        name: str,
        write: Callable[[], None],
        *,
        same_path: bool,
    ) -> str | None:
        """Run the write. The error text on failure: ``<path>: <error>`` where the script names the path, else the bare error."""
        try:
            write()
        except OSError as exc:
            return f"{store.path(name)}: {exc}" if same_path else str(exc)
        return None


__all__ = [
    "GOAL_STATE_FILE",
    "HEARTBEAT_FILE",
    "HISTORY_FILE",
    "HISTORY_KEEP",
    "REPO_STATUS_EVENTS",
    "REPO_STATUS_FILE",
    "HandlerThroughputTickRecord",
]

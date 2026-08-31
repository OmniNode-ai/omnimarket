# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State-store recovery adapter + epic-state reader [OMN-17017].

``ProtocolWatchdogRecoveryAdapter`` had no implementation anywhere, so live
recovery raised unconditionally, and ``ModelWatchdogResult`` hardcoded
``watchdog_log_path=None, dispatch_log_path=None`` while the contract promised a
friction event under ``.onex_state/friction/`` and an escalation line in
``.onex_state/dispatch-log/{date}.ndjson`` (2026-08-29 beta off-the-rails
analysis rev 2, §RC-J).

Both promises are implemented here, against the same durable state substrate the
wave scheduler uses. The reader implements the epic-state loading the request
model already advertised — ``$ONEX_STATE_DIR/epics/<epic_id>/state.yaml`` — so
the node's only CLI-expressible invocation (``--epic-id``) is reachable.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dispatch_watchdog_orchestrator.models.model_watchdog import (
    ModelWatchdogResult,
    ModelWaveTask,
)


class ModelWatchdogStatePaths(BaseModel):
    """Durable artefacts written for one watchdog check pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    watchdog_log_path: str = Field(
        description="watchdog.json snapshot written for this check."
    )
    dispatch_log_path: str = Field(
        description="NDJSON dispatch-log this check appended a line to."
    )


def resolve_state_dir(state_dir: str | None) -> Path:
    """Resolve the durable state root, failing fast when nothing is configured."""
    if state_dir:
        return Path(state_dir)
    configured = os.environ.get("ONEX_STATE_DIR")  # contract-config-ok: config
    if configured:
        return Path(configured)
    return Path(os.environ["OMNI_HOME"]) / ".onex_state"


class HandlerWatchdogEpicStateReader:
    """Reads a wave's task state from ``<state>/epics/<epic_id>/state.yaml``."""

    def __init__(self, *, state_dir: Path) -> None:
        self._state_dir = state_dir

    def state_path(self, epic_id: str) -> Path:
        return self._state_dir / "epics" / epic_id / "state.yaml"

    def read_wave_tasks(self, epic_id: str) -> tuple[ModelWaveTask, ...]:
        path = self.state_path(epic_id)
        if not path.is_file():
            raise FileNotFoundError(
                f"epic wave state not found at {path} — the watchdog reads "
                f"epics/{epic_id}/state.yaml under the resolved state dir; write "
                "it, or pass wave_tasks directly"
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"epic state at {path} must contain a mapping")
        rows = raw.get("wave_tasks")
        if not isinstance(rows, list):
            raise ValueError(f"epic state at {path} must declare a wave_tasks list")
        return tuple(ModelWaveTask.model_validate(row) for row in rows)


class HandlerWatchdogStateStore:
    """Recovery side effects recorded as durable state, not as claims."""

    def __init__(self, *, state_dir: Path, now_utc: datetime | None = None) -> None:
        self._state_dir = state_dir
        self._now_utc = now_utc

    # -- ProtocolWatchdogRecoveryAdapter ----------------------------------

    def cancel_task(self, task: ModelWaveTask) -> None:
        self._append_dispatch_log(
            {
                "event": "watchdog_cancel",
                "task_id": task.task_id,
                "ticket_id": task.ticket_id,
                "epic_id": None,
                "recorded_at_utc": self._timestamp(),
            }
        )

    def redispatch_task(self, task: ModelWaveTask) -> str:
        recovery_task_id = f"{task.task_id}-redispatch-{task.redispatch_count + 1}"
        self._append_dispatch_log(
            {
                "event": "watchdog_redispatch",
                "task_id": task.task_id,
                "ticket_id": task.ticket_id,
                "recovery_task_id": recovery_task_id,
                "epic_id": None,
                "recorded_at_utc": self._timestamp(),
            }
        )
        return recovery_task_id

    def escalate_to_blocked(self, task: ModelWaveTask) -> str | None:
        friction_dir = self._state_dir / "friction"
        friction_dir.mkdir(parents=True, exist_ok=True)
        stamp = self._timestamp().replace(":", "").replace("-", "")
        friction_path = friction_dir / f"watchdog-{task.task_id}-{stamp}.json"
        friction_path.write_text(
            json.dumps(
                {
                    "source": "node_dispatch_watchdog_orchestrator",
                    "task_id": task.task_id,
                    "ticket_id": task.ticket_id,
                    "team_name": task.team_name,
                    "redispatch_count": task.redispatch_count,
                    "reason": "max_redispatches exceeded",
                    "recorded_at_utc": self._timestamp(),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self._append_dispatch_log(
            {
                "event": "watchdog_escalated_to_blocked",
                "task_id": task.task_id,
                "ticket_id": task.ticket_id,
                "friction_event_path": str(friction_path),
                "epic_id": None,
                "recorded_at_utc": self._timestamp(),
            }
        )
        return str(friction_path)

    def record_check(self, result: ModelWatchdogResult) -> ModelWatchdogStatePaths:
        watchdog_dir = self._state_dir / "watchdog"
        watchdog_dir.mkdir(parents=True, exist_ok=True)
        watchdog_path = watchdog_dir / "watchdog.json"
        watchdog_path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        dispatch_log_path = self._append_dispatch_log(
            {
                "event": "watchdog_check",
                "epic_id": result.epic_id,
                "total_tasks": result.summary.total_tasks,
                "healthy": result.summary.healthy,
                "stalled": result.summary.stalled,
                "blocked": result.summary.blocked,
                "recorded_at_utc": result.check_timestamp_utc,
            }
        )
        return ModelWatchdogStatePaths(
            watchdog_log_path=str(watchdog_path),
            dispatch_log_path=str(dispatch_log_path),
        )

    # -- internals ---------------------------------------------------------

    def _append_dispatch_log(self, payload: dict[str, object]) -> Path:
        log_dir = self._state_dir / "dispatch-log"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{self._today()}.ndjson"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        return path

    def _now(self) -> datetime:
        return self._now_utc or datetime.now(UTC)

    def _today(self) -> str:
        return self._now().date().isoformat()

    def _timestamp(self) -> str:
        return self._now().isoformat().replace("+00:00", "Z")


__all__ = [
    "HandlerWatchdogEpicStateReader",
    "HandlerWatchdogStateStore",
    "ModelWatchdogStatePaths",
    "resolve_state_dir",
]

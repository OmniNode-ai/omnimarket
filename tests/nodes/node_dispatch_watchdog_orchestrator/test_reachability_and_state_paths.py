# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime reachability + real state paths for the dispatch watchdog [OMN-17017].

Before this repair the watchdog's only CLI-expressible request shape
(``--epic-id`` with no ``wave_tasks``) raised unconditionally, and the contract's
two state-path promises — a friction event under ``.onex_state/friction/`` and an
escalation line in ``.onex_state/dispatch-log/{date}.ndjson`` — were hardcoded
``None`` (2026-08-29 beta off-the-rails analysis rev 2, §RC-J / §4.3 A10 step 5).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dispatch_watchdog_orchestrator.handlers.handler_watchdog import (
    HandlerDispatchWatchdogOrchestrator,
    ModelWatchdogRequest,
)
from omnimarket.nodes.node_dispatch_watchdog_orchestrator.handlers.handler_watchdog_state_store import (
    HandlerWatchdogStateStore,
)
from omnimarket.nodes.node_dispatch_watchdog_orchestrator.models.model_watchdog import (
    EnumRecoveryAction,
    EnumTaskStatus,
    ModelWaveTask,
)

_NOW = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)
_STALE = "2026-08-30T11:00:00Z"


def _write_epic_state(
    state_dir: Path, epic_id: str, tasks: list[dict[str, object]]
) -> Path:
    epic_dir = state_dir / "epics" / epic_id
    epic_dir.mkdir(parents=True, exist_ok=True)
    path = epic_dir / "state.yaml"
    path.write_text(yaml.safe_dump({"wave_tasks": tasks}), encoding="utf-8")
    return path


@pytest.mark.unit
def test_epic_id_loads_wave_state_from_the_state_dir(tmp_path: Path) -> None:
    """``--epic-id`` does something observable instead of raising."""
    _write_epic_state(
        tmp_path,
        "OMN-17017",
        [
            {
                "task_id": "task-a",
                "ticket_id": "OMN-1",
                "team_name": "wave-1",
                "status": "in_progress",
                "last_activity_ts": _STALE,
            }
        ],
    )
    handler = HandlerDispatchWatchdogOrchestrator(now_utc=_NOW)

    result = handler.handle(
        ModelWatchdogRequest(
            epic_id="OMN-17017",
            state_dir=str(tmp_path),
            action=EnumRecoveryAction.REPORT,
        )
    )

    assert result.epic_id == "OMN-17017"
    assert result.summary.total_tasks == 1
    assert result.summary.stalled == 1
    assert result.stall_events[0].ticket_id == "OMN-1"


@pytest.mark.unit
def test_missing_epic_state_names_the_path(tmp_path: Path) -> None:
    handler = HandlerDispatchWatchdogOrchestrator(now_utc=_NOW)

    with pytest.raises(FileNotFoundError, match=re.escape("epics/OMN-404/state.yaml")):
        handler.handle(ModelWatchdogRequest(epic_id="OMN-404", state_dir=str(tmp_path)))


@pytest.mark.unit
def test_check_pass_writes_watchdog_json_and_dispatch_log(tmp_path: Path) -> None:
    """The contract's two state-path promises are now true — the paths are reported."""
    task = ModelWaveTask(
        task_id="task-a",
        ticket_id="OMN-1",
        team_name="wave-1",
        status=EnumTaskStatus.IN_PROGRESS,
        last_activity_ts=_STALE,
    )
    adapter = HandlerWatchdogStateStore(state_dir=tmp_path, now_utc=_NOW)
    handler = HandlerDispatchWatchdogOrchestrator(
        recovery_adapter=adapter, now_utc=_NOW
    )

    result = handler.handle(
        ModelWatchdogRequest(wave_tasks=(task,), action=EnumRecoveryAction.REPORT)
    )

    assert result.watchdog_log_path is not None
    assert result.dispatch_log_path is not None
    watchdog_json = json.loads(Path(result.watchdog_log_path).read_text())
    assert watchdog_json["summary"]["stalled"] == 1
    dispatch_lines = Path(result.dispatch_log_path).read_text().splitlines()
    assert json.loads(dispatch_lines[-1])["epic_id"] is None
    assert Path(result.dispatch_log_path).name == "2026-08-30.ndjson"


@pytest.mark.unit
def test_escalation_writes_a_friction_event(tmp_path: Path) -> None:
    task = ModelWaveTask(
        task_id="task-a",
        ticket_id="OMN-1",
        team_name="wave-1",
        status=EnumTaskStatus.IN_PROGRESS,
        last_activity_ts=_STALE,
        redispatch_count=2,
    )
    adapter = HandlerWatchdogStateStore(state_dir=tmp_path, now_utc=_NOW)
    handler = HandlerDispatchWatchdogOrchestrator(
        recovery_adapter=adapter, now_utc=_NOW
    )

    result = handler.handle(
        ModelWatchdogRequest(
            wave_tasks=(task,),
            action=EnumRecoveryAction.REDISPATCH,
            max_redispatches=2,
        )
    )

    action = result.recovery_actions[0]
    assert action.escalated_to_blocked is True
    assert action.friction_event_path is not None
    friction = json.loads(Path(action.friction_event_path).read_text())
    assert friction["ticket_id"] == "OMN-1"
    assert friction["redispatch_count"] == 2
    assert Path(action.friction_event_path).parent == tmp_path / "friction"


@pytest.mark.unit
def test_redispatch_returns_a_deterministic_recovery_task_id(tmp_path: Path) -> None:
    task = ModelWaveTask(
        task_id="task-a",
        ticket_id="OMN-1",
        team_name="wave-1",
        status=EnumTaskStatus.IN_PROGRESS,
        last_activity_ts=_STALE,
    )
    adapter = HandlerWatchdogStateStore(state_dir=tmp_path, now_utc=_NOW)
    handler = HandlerDispatchWatchdogOrchestrator(
        recovery_adapter=adapter, now_utc=_NOW
    )

    result = handler.handle(
        ModelWatchdogRequest(wave_tasks=(task,), action=EnumRecoveryAction.REDISPATCH)
    )

    assert result.stall_events[0].recovery_task_id == "task-a-redispatch-1"

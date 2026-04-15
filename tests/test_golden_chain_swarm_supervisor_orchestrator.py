# SPDX-License-Identifier: MIT
"""Golden chain tests for node_swarm_supervisor_orchestrator.

These tests use EventBusInmemory — zero infrastructure required.
They assert observable side effects: respawn log written, result fields correct.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_swarm_supervisor_orchestrator.handlers.handler_swarm_supervisor_orchestrator import (
    HandlerSwarmSupervisorOrchestrator,
)
from omnimarket.nodes.node_swarm_supervisor_orchestrator.models.model_swarm_supervisor_result import (
    EnumSupervisorStatus,
    EnumWorkerStatus,
)
from omnimarket.nodes.node_swarm_supervisor_orchestrator.models.model_swarm_supervisor_start_command import (
    ModelSwarmSupervisorStartCommand,
)


@pytest.fixture
def tmp_state_root(tmp_path: Path) -> Path:
    return tmp_path / "swarm_supervisor"


@pytest.fixture
def supervisor(tmp_state_root: Path) -> HandlerSwarmSupervisorOrchestrator:
    return HandlerSwarmSupervisorOrchestrator(
        event_bus=None,
        state_root=tmp_state_root,
    )


@pytest.mark.asyncio
async def test_no_workers_completes(
    supervisor: HandlerSwarmSupervisorOrchestrator,
) -> None:
    """Supervisor with no workers returns COMPLETE with zero counts."""
    cmd = ModelSwarmSupervisorStartCommand(
        correlation_id=uuid4(),
        worker_ids=[],
    )
    result = await supervisor.handle(cmd)

    assert result.overall_status == EnumSupervisorStatus.COMPLETE
    assert result.workers_supervised == 0
    assert result.respawns_issued == 0


@pytest.mark.asyncio
async def test_zombie_detection_and_respawn(
    supervisor: HandlerSwarmSupervisorOrchestrator,
    tmp_state_root: Path,
) -> None:
    """A worker pre-classified as zombie is respawned; log file is written."""
    from datetime import UTC, datetime, timedelta

    worker_id = "worker-abc-001"
    cmd = ModelSwarmSupervisorStartCommand(
        correlation_id=uuid4(),
        worker_ids=[worker_id],
        zombie_threshold_seconds=60,
        dry_run=True,
    )

    # Inject stale heartbeat directly into the supervisor's worker state
    # by pre-populating via a subclass shim that seeds workers before looping.
    stale_time = datetime.now(UTC) - timedelta(seconds=120)

    # Patch the worker table by overriding _supervision_loop entry state.
    original_loop = supervisor._supervision_loop

    async def patched_loop(command, workers, session_log_dir):  # type: ignore[override]
        workers[worker_id].last_heartbeat_at = stale_time
        return await original_loop(command, workers, session_log_dir)

    supervisor._supervision_loop = patched_loop  # type: ignore[method-assign]

    result = await supervisor.handle(cmd)

    assert result.overall_status == EnumSupervisorStatus.COMPLETE
    assert result.zombies_detected == 1
    assert result.respawns_issued == 1

    # Log file must exist and contain one entry
    log_file = tmp_state_root / str(cmd.correlation_id) / "respawn_log.jsonl"
    assert log_file.exists(), "respawn_log.jsonl not written"
    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["worker_id"] == worker_id
    assert entry["reason"] == EnumWorkerStatus.ZOMBIE
    assert entry["dry_run"] is True


@pytest.mark.asyncio
async def test_context_exhaustion_triggers_respawn(
    supervisor: HandlerSwarmSupervisorOrchestrator,
    tmp_state_root: Path,
) -> None:
    """Worker with context_usage_pct at threshold is respawned."""
    worker_id = "worker-ctx-001"
    cmd = ModelSwarmSupervisorStartCommand(
        correlation_id=uuid4(),
        worker_ids=[worker_id],
        context_exhaustion_pct=0.80,
        dry_run=True,
    )

    original_loop = supervisor._supervision_loop

    async def patched_loop(command, workers, session_log_dir):  # type: ignore[override]
        workers[worker_id].context_usage_pct = 0.85
        return await original_loop(command, workers, session_log_dir)

    supervisor._supervision_loop = patched_loop  # type: ignore[method-assign]

    result = await supervisor.handle(cmd)

    assert result.overall_status == EnumSupervisorStatus.COMPLETE
    assert result.respawns_issued == 1


@pytest.mark.asyncio
async def test_max_respawns_marks_abandoned(
    supervisor: HandlerSwarmSupervisorOrchestrator,
    tmp_state_root: Path,
) -> None:
    """Worker that has already hit max_respawn_attempts is abandoned, not respawned."""
    from datetime import UTC, datetime, timedelta

    worker_id = "worker-maxed-001"
    cmd = ModelSwarmSupervisorStartCommand(
        correlation_id=uuid4(),
        worker_ids=[worker_id],
        zombie_threshold_seconds=60,
        max_respawn_attempts=2,
        dry_run=True,
    )

    stale_time = datetime.now(UTC) - timedelta(seconds=120)
    original_loop = supervisor._supervision_loop

    async def patched_loop(command, workers, session_log_dir):  # type: ignore[override]
        workers[worker_id].last_heartbeat_at = stale_time
        workers[worker_id].respawn_count = 2  # already at max
        return await original_loop(command, workers, session_log_dir)

    supervisor._supervision_loop = patched_loop  # type: ignore[method-assign]

    result = await supervisor.handle(cmd)

    assert result.overall_status == EnumSupervisorStatus.COMPLETE
    assert result.respawns_issued == 0  # abandoned, not respawned

    log_file = tmp_state_root / str(cmd.correlation_id) / "respawn_log.jsonl"
    assert log_file.exists()
    entry = json.loads(log_file.read_text().strip())
    assert entry["action"] == "abandoned"


@pytest.mark.asyncio
async def test_session_summary_written(
    supervisor: HandlerSwarmSupervisorOrchestrator,
    tmp_state_root: Path,
) -> None:
    """Session summary JSON is always written on exit."""
    cmd = ModelSwarmSupervisorStartCommand(
        correlation_id=uuid4(),
        worker_ids=[],
    )
    await supervisor.handle(cmd)

    summary_file = tmp_state_root / str(cmd.correlation_id) / "summary.json"
    assert summary_file.exists()
    data = json.loads(summary_file.read_text())
    assert data["overall_status"] == EnumSupervisorStatus.COMPLETE

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real effect boundary for the wave scheduler [OMN-17017].

``ProtocolWaveDispatcher`` had no implementation anywhere, so every live run
raised ``RuntimeError("dispatcher adapter required when dry_run is false")``
(2026-08-29 beta off-the-rails analysis rev 2, §RC-J / §4.3 A10 step 2).

The chosen substrate is durable dispatch-lifecycle records under
``$ONEX_STATE_DIR`` — append-only NDJSON, one record per lifecycle transition,
plus an observed-outcome read-back. Nothing is inferred: a ticket with no
observed outcome record is reported UNREPORTED and stays visibly pending.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_wave_scheduler_orchestrator.handlers.handler_wave_dispatch_state_store import (
    HandlerWaveDispatchStateStore,
)
from omnimarket.nodes.node_wave_scheduler_orchestrator.models.model_wave_scheduler_result import (
    EnumDispatchLifecyclePhase,
    EnumTicketExecutionStatus,
    ModelWaveAssignment,
)


def _write_observed(state_dir: Path, run_id: str, ticket_id: str, status: str) -> None:
    observed_dir = state_dir / "wave_scheduler" / run_id / "observed"
    observed_dir.mkdir(parents=True, exist_ok=True)
    (observed_dir / f"{ticket_id}.json").write_text(
        json.dumps({"ticket_id": ticket_id, "status": status}),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_dispatch_wave_writes_lifecycle_records(tmp_path: Path) -> None:
    dispatcher = HandlerWaveDispatchStateStore(state_dir=tmp_path, run_id="run-1")
    assignment = ModelWaveAssignment(
        wave_id=0,
        ticket_ids=("OMN-1", "OMN-2"),
        repo_assignments=(("OMN-1", "omnimarket"),),
    )
    _write_observed(tmp_path, "run-1", "OMN-1", "completed")

    statuses = dispatcher.dispatch_wave(assignment)

    assert statuses == {"OMN-1": EnumTicketExecutionStatus.COMPLETED}
    lifecycle = tmp_path / "wave_scheduler" / "run-1" / "lifecycle.ndjson"
    records = [json.loads(line) for line in lifecycle.read_text().splitlines()]
    phases = [(record["ticket_id"], record["phase"]) for record in records]
    assert ("OMN-1", EnumDispatchLifecyclePhase.DISPATCH_REQUESTED.value) in phases
    assert ("OMN-1", EnumDispatchLifecyclePhase.OBSERVED.value) in phases
    assert ("OMN-2", EnumDispatchLifecyclePhase.DISPATCH_REQUESTED.value) in phases
    assert ("OMN-2", EnumDispatchLifecyclePhase.UNREPORTED.value) in phases
    assert dispatcher.lifecycle_path == lifecycle


@pytest.mark.unit
def test_lifecycle_records_are_append_only(tmp_path: Path) -> None:
    dispatcher = HandlerWaveDispatchStateStore(state_dir=tmp_path, run_id="run-2")
    first = ModelWaveAssignment(wave_id=0, ticket_ids=("OMN-1",))
    second = ModelWaveAssignment(wave_id=1, ticket_ids=("OMN-2",))

    dispatcher.dispatch_wave(first)
    dispatcher.dispatch_wave(second)

    lifecycle = tmp_path / "wave_scheduler" / "run-2" / "lifecycle.ndjson"
    records = [json.loads(line) for line in lifecycle.read_text().splitlines()]
    assert len(records) == 4
    assert {record["wave_id"] for record in records} == {0, 1}
    assert all(record["run_id"] == "run-2" for record in records)


@pytest.mark.unit
def test_observed_outcome_with_unknown_status_fails_fast(tmp_path: Path) -> None:
    dispatcher = HandlerWaveDispatchStateStore(state_dir=tmp_path, run_id="run-3")
    _write_observed(tmp_path, "run-3", "OMN-1", "green-ish")

    with pytest.raises(ValueError, match="green-ish"):
        dispatcher.dispatch_wave(ModelWaveAssignment(wave_id=0, ticket_ids=("OMN-1",)))

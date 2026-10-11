# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1b (OMN-20803): a run first seen in progress whose completion lands after
the cursor moved still emits its finished event."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationRunOutcome,
    EnumAutomationRunPhase,
)
from omnimarket.nodes.node_github_schedule_observer_effect.handlers.schedule_observer_state_store import (
    FileScheduleObserverStateStore,
)
from tests.nodes.node_github_schedule_observer_effect.support import (
    RecordedTransport,
    current_clone,
    handler_for,
    request_at,
    run_events,
)

PROCESS = "github/example-repo/chain-canary"
RUN_URL = "/repos/example-owner/example-repo/actions/runs/3001"


async def test_gh_schedule_outstanding_runs_complete_after_cursor_moves(
    tmp_path: Path,
) -> None:
    clone = current_clone("chain-canary.yml")

    # 04:00 -- run 3001 is in progress.
    tick1 = await handler_for(RecordedTransport("outstanding_tick1"), clone).handle(
        request_at("2026-10-10T04:00:00Z", tmp_path)
    )
    (started,) = run_events(tick1)
    assert started.run_id == f"{PROCESS}:3001"
    assert started.phase is EnumAutomationRunPhase.STARTED

    # 05:00 -- a newer run completed; 3001 is still listed, still unfinished.
    tick2 = await handler_for(RecordedTransport("outstanding_tick2"), clone).handle(
        request_at("2026-10-10T05:00:00Z", tmp_path)
    )
    assert [(e.run_id, e.phase) for e in run_events(tick2)] == [
        (f"{PROCESS}:3002", EnumAutomationRunPhase.FINISHED)
    ], "3001 is not reported twice while it is unfinished"

    # 06:00 -- the cursor is past 3001, so the listing no longer holds it; it
    # finished meanwhile and is read by id.
    transport3 = RecordedTransport("outstanding_tick3")
    tick3 = await handler_for(transport3, clone).handle(
        request_at("2026-10-10T06:00:00Z", tmp_path)
    )
    assert RUN_URL in transport3.paths, "the outstanding run is read by id"
    (finished,) = run_events(tick3)
    assert finished.run_id == f"{PROCESS}:3001"
    assert finished.phase is EnumAutomationRunPhase.FINISHED
    assert finished.outcome is EnumAutomationRunOutcome.OK
    assert finished.started_at.isoformat() == "2026-10-10T03:00:00+00:00"
    assert finished.finished_at is not None
    assert finished.finished_at.isoformat() == "2026-10-10T05:20:00+00:00"

    state = FileScheduleObserverStateStore(tmp_path / "state.json").load()
    assert state.repositories["example-owner/example-repo"].outstanding == {}, (
        "a finished run is no longer read"
    )


async def test_gh_schedule_outstanding_runs_complete_unfinished_stays_outstanding(
    tmp_path: Path,
) -> None:
    clone = current_clone("chain-canary.yml")
    await handler_for(RecordedTransport("outstanding_tick1"), clone).handle(
        request_at("2026-10-10T04:00:00Z", tmp_path)
    )
    await handler_for(RecordedTransport("outstanding_tick2"), clone).handle(
        request_at("2026-10-10T05:00:00Z", tmp_path)
    )
    state = FileScheduleObserverStateStore(tmp_path / "state.json").load()
    outstanding = state.repositories["example-owner/example-repo"].outstanding
    assert list(outstanding) == [f"{PROCESS}:3001"], (
        "positive control: an unfinished run stays outstanding until it completes"
    )

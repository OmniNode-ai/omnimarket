# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 (OMN-20803): a paginated runs response emits one event per scheduled run,
resumes from its cursor after downtime, and a disabled workflow emits MISSED."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
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
    verdict_events,
)

PROCESS = "github/example-repo/chain-canary"
RUNS_FIRST = (
    "/repos/example-owner/example-repo/actions/runs?event=schedule"
    "&created=%3E%3D2026-10-09T04:00:00Z&per_page=100&page={page}"
)


async def test_gh_schedule_observer_paginated_cursor_one_event_per_run(
    tmp_path: Path,
) -> None:
    transport = RecordedTransport("paginated_first_tick")
    handler = handler_for(transport, current_clone("chain-canary.yml"))

    output = await handler.handle(request_at("2026-10-10T04:00:00Z", tmp_path))

    events = run_events(output)
    assert [e.run_id for e in events] == [
        f"{PROCESS}:1001",
        f"{PROCESS}:1002",
        f"{PROCESS}:1003",
    ], "one event per scheduled run of the declared workflow, across both pages"
    assert all(e.phase is EnumAutomationRunPhase.FINISHED for e in events)
    assert [e.outcome for e in events] == [
        EnumAutomationRunOutcome.OK,
        EnumAutomationRunOutcome.FAILED,
        EnumAutomationRunOutcome.OK,
    ]
    assert RUNS_FIRST.format(page=1) in transport.paths
    assert RUNS_FIRST.format(page=2) in transport.paths, "page 2 was read"
    assert verdict_events(output) == []


async def test_gh_schedule_observer_paginated_cursor_resumes_after_downtime(
    tmp_path: Path,
) -> None:
    first = RecordedTransport("paginated_first_tick")
    await handler_for(first, current_clone("chain-canary.yml")).handle(
        request_at("2026-10-10T04:00:00Z", tmp_path)
    )
    saved = FileScheduleObserverStateStore(tmp_path / "state.json").load()
    assert saved.repositories["example-owner/example-repo"].runs_cursor is not None

    # The observer was down for three hours; a new process reads the cursor.
    resumed = RecordedTransport("paginated_resume_tick")
    output = await handler_for(resumed, current_clone("chain-canary.yml")).handle(
        request_at("2026-10-10T07:00:00Z", tmp_path)
    )

    assert resumed.paths == [
        "/repos/example-owner/example-repo/actions/runs?event=schedule"
        "&created=%3E%3D2026-10-10T03:00:21Z&per_page=100&page=1"
    ], "the read starts at the persisted cursor and skips the daily state read"
    assert [e.run_id for e in run_events(output)] == [
        f"{PROCESS}:1004",
        f"{PROCESS}:1005",
    ], "the run at the cursor (1003) is read again and not reported again"


async def test_gh_schedule_observer_paginated_cursor_disabled_workflow_is_missed(
    tmp_path: Path,
) -> None:
    transport = RecordedTransport("disabled_workflow")
    handler = handler_for(transport, current_clone("chain-canary.yml"))

    output = await handler.handle(request_at("2026-10-10T04:00:00Z", tmp_path))

    (verdict,) = verdict_events(output)
    assert verdict.process_id == PROCESS
    assert verdict.verdict is EnumAutomationLivenessVerdict.MISSED
    assert verdict.reason is EnumAutomationLivenessReason.WORKFLOW_DISABLED
    assert "disabled_inactivity" in verdict.detail
    assert run_events(output) == [], "a disabled workflow has no runs to report"


async def test_gh_schedule_observer_paginated_cursor_states_read_once_a_day(
    tmp_path: Path,
) -> None:
    await handler_for(
        RecordedTransport("disabled_workflow"), current_clone("chain-canary.yml")
    ).handle(request_at("2026-10-10T04:00:00Z", tmp_path))

    within_a_day = RecordedTransport("empty_runs")
    await handler_for(within_a_day, current_clone("chain-canary.yml")).handle(
        request_at("2026-10-10T07:00:00Z", tmp_path)
    )
    assert not any("/actions/workflows" in p for p in within_a_day.paths)

    next_day = RecordedTransport("disabled_workflow")
    output = await handler_for(next_day, current_clone("chain-canary.yml")).handle(
        request_at("2026-10-11T04:30:00Z", tmp_path)
    )
    assert any("/actions/workflows" in p for p in next_day.paths)
    assert [v.verdict for v in verdict_events(output)] == [
        EnumAutomationLivenessVerdict.MISSED
    ], "still disabled a day later, still MISSED"

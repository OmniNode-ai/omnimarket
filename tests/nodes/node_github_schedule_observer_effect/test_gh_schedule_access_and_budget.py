# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-20803): an inaccessible repository or an exhausted budget reports
UNOBSERVABLE, never zero runs."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    ModelAutomationLivenessVerdictEvent,
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


async def _tick(
    fixture: str, tmp_path: Path, **extra: object
) -> tuple[RecordedTransport, list[ModelAutomationLivenessVerdictEvent]]:
    transport = RecordedTransport(fixture)
    output = await handler_for(transport, current_clone("chain-canary.yml")).handle(
        request_at("2026-10-10T04:00:00Z", tmp_path, **extra)
    )
    assert run_events(output) == [], "no run is invented for an unreadable source"
    return transport, verdict_events(output)


async def test_gh_schedule_access_and_budget_inaccessible_repository(
    tmp_path: Path,
) -> None:
    _, verdicts = await _tick("inaccessible_repository", tmp_path)

    (verdict,) = verdicts
    assert verdict.process_id == PROCESS
    assert verdict.verdict is EnumAutomationLivenessVerdict.UNOBSERVABLE
    assert verdict.reason is EnumAutomationLivenessReason.EVIDENCE_UNREADABLE
    assert "HTTP 404" in verdict.detail
    cursor = FileScheduleObserverStateStore(tmp_path / "state.json").load()
    assert cursor.repositories["example-owner/example-repo"].runs_cursor is None, (
        "the cursor does not move over a read that failed"
    )


async def test_gh_schedule_access_and_budget_rate_limit_is_incomplete(
    tmp_path: Path,
) -> None:
    _, verdicts = await _tick("rate_limited", tmp_path)

    (verdict,) = verdicts
    assert verdict.verdict is EnumAutomationLivenessVerdict.UNOBSERVABLE
    assert verdict.reason is EnumAutomationLivenessReason.READ_INCOMPLETE
    assert "HTTP 403" in verdict.detail


async def test_gh_schedule_access_and_budget_floor_stops_the_next_call(
    tmp_path: Path,
) -> None:
    transport, verdicts = await _tick("budget_floor", tmp_path)

    assert len(transport.requests) == 1, (
        "the first response left 120 remaining, under the floor of 500: "
        "the runs read made no call"
    )
    (verdict,) = verdicts
    assert verdict.verdict is EnumAutomationLivenessVerdict.UNOBSERVABLE
    assert verdict.reason is EnumAutomationLivenessReason.READ_INCOMPLETE
    assert "under the contract floor" in verdict.detail


async def test_gh_schedule_access_and_budget_truncated_listing_is_incomplete(
    tmp_path: Path,
) -> None:
    _, verdicts = await _tick("truncated_listing", tmp_path)

    (verdict,) = verdicts
    assert verdict.reason is EnumAutomationLivenessReason.READ_INCOMPLETE
    assert "lists 1 of 1500" in verdict.detail


async def test_gh_schedule_access_and_budget_page_limit_is_incomplete(
    tmp_path: Path,
) -> None:
    _, verdicts = await _tick("paginated_first_tick", tmp_path, max_pages=1)

    (verdict,) = verdicts
    assert verdict.reason is EnumAutomationLivenessReason.READ_INCOMPLETE
    assert "more than 1 pages" in verdict.detail


async def test_gh_schedule_access_and_budget_healthy_read_has_no_verdict(
    tmp_path: Path,
) -> None:
    """Positive control: a readable repository raises no UNOBSERVABLE."""
    _, verdicts = await _tick("quiet_schedule", tmp_path)

    assert verdicts == []

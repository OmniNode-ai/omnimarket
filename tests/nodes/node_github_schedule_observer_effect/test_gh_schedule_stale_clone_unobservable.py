# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 (OMN-20803): a clone whose head differs from its remote default branch
makes its workflows UNOBSERVABLE."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
)
from omnimarket.nodes.node_github_schedule_observer_effect.protocols import (
    ScheduleCloneError,
)
from tests.nodes.node_github_schedule_observer_effect.support import (
    RecordedTransport,
    current_clone,
    handler_for,
    request_at,
    run_events,
    verdict_events,
)


async def test_gh_schedule_stale_clone_unobservable_for_every_workflow(
    tmp_path: Path,
) -> None:
    transport = RecordedTransport()
    stale = current_clone("chain-canary.yml", head="a" * 40, remote="b" * 40)

    output = await handler_for(transport, stale).handle(
        request_at("2026-10-10T04:00:00Z", tmp_path, runtime_paths=("src/",))
    )

    verdicts = verdict_events(output)
    assert sorted(v.process_id for v in verdicts) == [
        "github/example-repo/chain-canary",
        "github/example-repo/deploy-on-merge",
    ], "every workflow of the repository, scheduled and event-triggered"
    assert all(
        v.verdict is EnumAutomationLivenessVerdict.UNOBSERVABLE for v in verdicts
    )
    assert all(v.reason is EnumAutomationLivenessReason.SOURCE_STALE for v in verdicts)
    assert "aaaaaaaaaaaa" in verdicts[0].detail
    assert "bbbbbbbbbbbb" in verdicts[0].detail
    assert transport.requests == [], "a stale clone is not trusted and nothing is read"
    assert run_events(output) == []


async def test_gh_schedule_stale_clone_unobservable_current_clone_is_read(
    tmp_path: Path,
) -> None:
    """Positive control: the same request with a current clone reads GitHub."""
    transport = RecordedTransport("quiet_schedule")

    output = await handler_for(transport, current_clone("chain-canary.yml")).handle(
        request_at("2026-10-10T04:00:00Z", tmp_path)
    )

    assert len(transport.requests) == 2
    assert verdict_events(output) == []


async def test_gh_schedule_stale_clone_unobservable_unreadable_clone(
    tmp_path: Path,
) -> None:
    transport = RecordedTransport()

    output = await handler_for(
        transport, ScheduleCloneError("no clone at example-repo")
    ).handle(request_at("2026-10-10T04:00:00Z", tmp_path))

    verdicts = verdict_events(output)
    assert len(verdicts) == 2
    assert all(
        v.reason is EnumAutomationLivenessReason.EVIDENCE_UNREADABLE for v in verdicts
    )
    assert transport.requests == []

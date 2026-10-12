# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 (OMN-20803): a runtime-affecting merge whose bus publication was lost
still opens its expectation from the closed-pull-request read, and one that was
published opens no second."""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationEmitter,
    EnumAutomationLivenessReason,
    EnumAutomationRunPhase,
)
from tests.nodes.node_github_schedule_observer_effect.support import (
    RecordedTransport,
    current_clone,
    handler_for,
    request_at,
    run_events,
    verdict_events,
)

PROCESS = "github/example-repo/deploy-on-merge"
BASE = "/repos/example-owner/example-repo"
PUBLISHED_MERGE = "a" * 40  # PR 201, merged runtime-affecting; the bus carried it
LOST_MERGE = "b" * 40  # PR 202, merged runtime-affecting; the publication was lost
DOCS_MERGE = "c" * 40  # PR 203, merged, touches only docs


async def test_gh_observer_reconciles_lost_triggers_opens_only_the_lost_one(
    tmp_path: Path,
) -> None:
    transport = RecordedTransport("quiet_schedule", "closed_pulls")
    handler = handler_for(transport, current_clone("chain-canary.yml"))

    output = await handler.handle(
        request_at(
            "2026-10-10T04:00:00Z",
            tmp_path,
            runtime_paths=("src/runtime/",),
            published_trigger_keys=[PUBLISHED_MERGE],
        )
    )

    (expectation,) = run_events(output)
    assert expectation.process_id == PROCESS
    assert expectation.run_id == f"{PROCESS}:{LOST_MERGE}"
    assert expectation.phase is EnumAutomationRunPhase.STARTED
    assert expectation.started_at.isoformat() == "2026-10-10T02:30:00+00:00"
    assert expectation.work_unit == "example-owner/example-repo#202"
    assert expectation.emitter is EnumAutomationEmitter.OBSERVER
    assert verdict_events(output) == []
    files_read = [p for p in transport.paths if "/pulls/" in p]
    assert files_read == [
        f"{BASE}/pulls/202/files?per_page=100&page=1",
        f"{BASE}/pulls/203/files?per_page=100&page=1",
    ] or files_read == [
        f"{BASE}/pulls/203/files?per_page=100&page=1",
        f"{BASE}/pulls/202/files?per_page=100&page=1",
    ], (
        "a published merge (201) is not read for files, an unmerged PR (204) is not a merge"
    )


async def test_gh_observer_reconciles_lost_triggers_opens_no_second_on_the_next_tick(
    tmp_path: Path,
) -> None:
    request = request_at(
        "2026-10-10T04:00:00Z",
        tmp_path,
        runtime_paths=("src/runtime/",),
        published_trigger_keys=[PUBLISHED_MERGE],
    )
    await handler_for(
        RecordedTransport("quiet_schedule", "closed_pulls"),
        current_clone("chain-canary.yml"),
    ).handle(request)

    # An hour later the same merges are still inside the closed-PR window; the
    # recorded listing is read again and nothing new opens.
    again = RecordedTransport("empty_runs", "closed_pulls")
    output = await handler_for(again, current_clone("chain-canary.yml")).handle(
        request_at(
            "2026-10-10T05:00:00Z",
            tmp_path,
            runtime_paths=("src/runtime/",),
            published_trigger_keys=[PUBLISHED_MERGE],
        )
    )

    assert run_events(output) == [], "the lost merge's expectation opened once"
    assert [p for p in again.paths if "/pulls/" in p and "/files" in p] == [], (
        "an examined merge is not read for files again"
    )


async def test_gh_observer_reconciles_lost_triggers_without_runtime_paths_reads_nothing(
    tmp_path: Path,
) -> None:
    """Positive control: a repository with no runtime paths is not reconciled."""
    transport = RecordedTransport("quiet_schedule")

    output = await handler_for(transport, current_clone("chain-canary.yml")).handle(
        request_at("2026-10-10T04:00:00Z", tmp_path)
    )

    assert not any("/pulls" in p for p in transport.paths)
    assert run_events(output) == []


async def test_gh_observer_reconciles_lost_triggers_read_failure_is_unobservable(
    tmp_path: Path,
) -> None:
    transport = RecordedTransport("quiet_schedule", "inaccessible_pulls")

    output = await handler_for(transport, current_clone("chain-canary.yml")).handle(
        request_at("2026-10-10T04:00:00Z", tmp_path, runtime_paths=("src/runtime/",))
    )

    (verdict,) = verdict_events(output)
    assert verdict.process_id == PROCESS
    assert verdict.reason is EnumAutomationLivenessReason.EVIDENCE_UNREADABLE
    assert run_events(output) == []

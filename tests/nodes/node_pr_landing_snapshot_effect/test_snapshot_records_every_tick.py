# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The snapshot step fills the write facts from the watcher and keeps every tick on the bus (OMN-23409, AC1).

Two consecutive ticks go through the step. Each PR's node id and its head's failed run ids come
from the watcher record of that tick; after the second tick the first tick's facts are still read
back from the recorded topic, through the tick orchestrator's own replay source, and replay equal.

Failing control: a record log that keeps only the last tick (what the script's last-facts file
does) loses the first tick, and the recovery check goes red under it.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel

from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
    contract_topics,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_sources import (
    InMemoryWatcherSource,
    watcher_key,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotStatus,
    ModelLandingSnapshotRequest,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
    RedpandaSnapshotSource,
    run_snapshots,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
TICK = (
    REPO / "tests" / "fixtures" / "pr_landing_tick_replay" / "tick_725_omnimarket.json"
)
RECORDED_TOPIC, FAILED_TOPIC = contract_topics()
# The overlay value the act-mode writer's rerun reads as failed; the test injects it as an overlay would.
FAILED_CONCLUSIONS = ("failure", "cancelled", "timed_out")


def _tick(n: int) -> ModelTickSnapshot:
    base = ModelTickSnapshot.model_validate_json(TICK.read_text(encoding="utf-8"))
    return base.model_copy(
        update={
            "facts": base.facts.model_copy(update={"tick": n}),
            "decision": base.decision.model_copy(
                update={
                    "tick": n,
                    "next_state": base.decision.next_state.model_copy(
                        update={"last_tick": n}
                    ),
                }
            ),
        }
    )


def _watcher(tick: ModelTickSnapshot, run_base: int) -> InMemoryWatcherSource:
    """One watcher record per PR, as the watcher writes it: node id, CI head and red_facts rows."""
    records: dict[str, dict[str, object]] = {}
    for i, pr in enumerate(tick.facts.prs):
        records[watcher_key(pr.pr)] = {
            "facts": {"node_id": f"PR_node_{i}", "head_sha": pr.head_sha},
            "ci": {
                "sha": pr.head_sha,
                "read_at": "2026-11-10T01:00:00Z",
                "red_facts": [
                    [
                        "Tests (Split 1/20)",
                        "failure",
                        str(run_base + i),
                        "2026-11-10T00:59:00Z",
                    ],
                    ["lint", "cancelled", str(run_base + i), "2026-11-10T00:59:00Z"],
                    [
                        "advisory",
                        "neutral",
                        str(run_base + 500 + i),
                        "2026-11-10T00:59:00Z",
                    ],
                ],
            },
        }
    return InMemoryWatcherSource(records)


class _TopicLog(InMemoryTickPublisher):
    """The recorded topic as an append-only log: every record stays."""

    def records(self) -> list[str]:
        return [e.model_dump_json() for t, e in self.published if t == RECORDED_TOPIC]


class _LastOnlyLog(_TopicLog):
    """Failing control keep_only_last: each record replaces the one before (the script's last-facts file)."""

    def publish(self, topic: str, event: BaseModel) -> None:
        self.published = [(t, e) for t, e in self.published if t != topic]
        super().publish(topic, event)


class _Reader:
    def __init__(self, log: _TopicLog) -> None:
        self._log = log

    def read(self, max_records: int, timeout_s: float) -> Iterable[str]:
        return self._log.records()[:max_records]


def _record_two_ticks(log: _TopicLog) -> tuple[ModelTickSnapshot, ModelTickSnapshot]:
    first, second = _tick(725), _tick(726)
    for tick, run_base in ((first, 42_000_000_000), (second, 43_000_000_000)):
        result = HandlerPrLandingSnapshot(
            failed_conclusions=FAILED_CONCLUSIONS,
            watcher=_watcher(tick, run_base),
            publisher=log,
        ).handle(
            ModelLandingSnapshotRequest(
                correlation_id=uuid4(), facts=tick.facts, decision=tick.decision
            )
        )
        assert result.status is EnumSnapshotStatus.RECORDED
    return first, second


def _first_tick_is_recoverable_after_the_second(log: _TopicLog) -> None:
    first, _ = _record_two_ticks(log)
    recovered = RedpandaSnapshotSource(
        _Reader(log), max_snapshots=10, poll_timeout_s=1.0
    ).snapshots()
    by_tick = {s.facts.tick: s for s in recovered}
    assert sorted(by_tick) == [725, 726]
    kept = by_tick[725]
    assert [p.pr for p in kept.facts.prs] == [p.pr for p in first.facts.prs]
    assert kept.facts.prs[0].pr_node_id == "PR_node_0"
    assert kept.facts.prs[0].failed_run_ids == (42_000_000_000,)


def test_two_consecutive_ticks_leave_both_ticks_recoverable() -> None:
    _first_tick_is_recoverable_after_the_second(_TopicLog())


def test_failing_control_a_log_that_keeps_only_the_last_tick_loses_the_first() -> None:
    with pytest.raises(AssertionError):
        _first_tick_is_recoverable_after_the_second(_LastOnlyLog())


def test_each_pr_gets_its_node_id_and_only_its_failed_run_ids() -> None:
    log = _TopicLog()
    _record_two_ticks(log)
    recorded = [ModelTickSnapshot.model_validate_json(r) for r in log.records()]
    for snap, run_base in zip(recorded, (42_000_000_000, 43_000_000_000), strict=True):
        for i, pr in enumerate(snap.facts.prs):
            assert pr.pr_node_id == f"PR_node_{i}"
            # failure and cancelled share one run; neutral is not a failed conclusion
            assert pr.failed_run_ids == (run_base + i,)
    assert (
        recorded[1].facts.prs[0].failed_run_ids
        != recorded[0].facts.prs[0].failed_run_ids
    )


def test_a_watcher_record_of_another_head_gives_no_run_ids_but_the_node_id() -> None:
    tick = _tick(725)
    pr = tick.facts.prs[0]
    watcher = InMemoryWatcherSource(
        {
            watcher_key(pr.pr): {
                "facts": {"node_id": "PR_moved"},
                "ci": {
                    "sha": "f" * 40,
                    "red_facts": [["Tests", "failure", "7", "2026-11-10T00:00:00Z"]],
                },
            }
        }
    )
    result = HandlerPrLandingSnapshot(
        failed_conclusions=FAILED_CONCLUSIONS, watcher=watcher
    ).handle(
        ModelLandingSnapshotRequest(
            correlation_id=uuid4(), facts=tick.facts, decision=tick.decision
        )
    )
    assert result.snapshot is not None
    filled = result.snapshot.facts.prs[0]
    assert filled.pr_node_id == "PR_moved"
    assert filled.failed_run_ids == ()
    assert result.other_head_prs == (pr.pr,)
    assert len(result.unread_prs) == len(tick.facts.prs) - 1


def test_the_recorded_ticks_replay_equal_through_the_tick_orchestrator() -> None:
    log = _TopicLog()
    _record_two_ticks(log)
    source = RedpandaSnapshotSource(_Reader(log), max_snapshots=10, poll_timeout_s=1.0)
    results = run_snapshots(
        HandlerPrLandingTickOrchestrator(), source, EnumTickMode.REPLAY
    )
    assert [r.tick for r in results] == [725, 726]
    assert all(r.status is EnumTickStatus.COMPARED and r.equal for r in results)
    assert all(r.writes_performed == 0 for r in results)

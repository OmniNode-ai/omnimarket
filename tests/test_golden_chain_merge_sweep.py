"""Golden chain test for node_merge_sweep.

Verifies PR classification logic, failure history tracking, event bus wiring,
and lifecycle-ordered Track A merge sequencing.
"""

from __future__ import annotations

import json

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
    FailureCategory,
    FailureHistoryEntry,
    MergeSweepRequest,
    NodeMergeSweep,
    PRInfo,
    PRTrack,
)

CMD_TOPIC = "onex.cmd.omnimarket.merge-sweep-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.merge-sweep-completed.v1"


@pytest.mark.unit
class TestMergeSweepGoldenChain:
    """Golden chain: command -> classify -> completion event."""

    async def test_merge_ready_pr_classified_track_a(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A mergeable, green, approved PR should go to Track A."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=42,
            title="feat: add feature",
            repo="OmniNode-ai/omniclaude",
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            review_decision="APPROVED",
            required_checks_pass=True,
        )
        request = MergeSweepRequest(prs=[pr])
        result = handler.handle(request)

        assert result.status == "queued"
        assert len(result.track_a_merge) == 1
        assert result.track_a_merge[0].pr.number == 42
        assert result.track_a_merge[0].failure_categories == []

    async def test_behind_pr_classified_track_a_update(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A mergeable but BEHIND PR should go to Track A-update."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=10,
            title="fix: typo",
            repo="OmniNode-ai/omnibase_core",
            mergeable="MERGEABLE",
            merge_state_status="BEHIND",
            review_decision="APPROVED",
            required_checks_pass=True,
        )
        request = MergeSweepRequest(prs=[pr])
        result = handler.handle(request)

        assert len(result.track_a_update) == 1
        assert result.track_a_update[0].track == PRTrack.A_UPDATE
        assert (
            FailureCategory.BRANCH_STALE.value
            in result.track_a_update[0].failure_categories
        )

    async def test_blocked_green_pr_classified_track_a_resolve(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A MERGEABLE + BLOCKED + GREEN PR goes to Track A-resolve."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=517,
            title="feat: visibility toggles",
            repo="OmniNode-ai/omnidash",
            mergeable="MERGEABLE",
            merge_state_status="BLOCKED",
            review_decision=None,
            required_checks_pass=True,
        )
        request = MergeSweepRequest(prs=[pr])
        result = handler.handle(request)

        assert len(result.track_a_resolve) == 1
        assert result.track_a_resolve[0].track == PRTrack.A_RESOLVE
        assert (
            FailureCategory.THREADS_BLOCKED.value
            in result.track_a_resolve[0].failure_categories
        )

    async def test_conflicting_pr_classified_track_b(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A conflicting PR should go to Track B for polish."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=99,
            title="chore: update deps",
            repo="OmniNode-ai/omnidash",
            mergeable="CONFLICTING",
            merge_state_status="DIRTY",
            review_decision="APPROVED",
            required_checks_pass=True,
        )
        request = MergeSweepRequest(prs=[pr])
        result = handler.handle(request)

        assert len(result.track_b_polish) == 1
        assert "conflicts" in result.track_b_polish[0].reason
        assert (
            FailureCategory.CONFLICT.value
            in result.track_b_polish[0].failure_categories
        )

    async def test_ci_failing_pr_has_failure_categories(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A PR with CI failures should have ci_test in failure_categories."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=200,
            title="feat: broken tests",
            repo="OmniNode-ai/omniclaude",
            mergeable="MERGEABLE",
            merge_state_status="BLOCKED",
            required_checks_pass=False,
        )
        request = MergeSweepRequest(prs=[pr])
        result = handler.handle(request)

        assert len(result.track_b_polish) == 1
        assert (
            FailureCategory.CI_TEST.value in result.track_b_polish[0].failure_categories
        )

    async def test_draft_pr_skipped(self, event_bus: EventBusInmemory) -> None:
        """Draft PRs should be skipped."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=5,
            title="wip: new feature",
            repo="OmniNode-ai/omniclaude",
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            is_draft=True,
        )
        request = MergeSweepRequest(prs=[pr])
        result = handler.handle(request)

        assert result.status == "nothing_to_merge"
        assert len(result.skipped) == 1

    async def test_max_total_merges_cap(self, event_bus: EventBusInmemory) -> None:
        """max_total_merges should cap Track A candidates."""
        handler = NodeMergeSweep()
        prs = [
            PRInfo(
                number=i,
                title=f"PR #{i}",
                repo="OmniNode-ai/test",
                mergeable="MERGEABLE",
                merge_state_status="CLEAN",
                review_decision="APPROVED",
                required_checks_pass=True,
            )
            for i in range(5)
        ]
        request = MergeSweepRequest(prs=prs, max_total_merges=2)
        result = handler.handle(request)

        assert len(result.track_a_merge) == 2
        assert len(result.skipped) == 3

    async def test_skip_polish_flag(self, event_bus: EventBusInmemory) -> None:
        """--skip-polish should move Track B PRs to skip."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=77,
            title="fix: broken",
            repo="OmniNode-ai/test",
            mergeable="CONFLICTING",
            merge_state_status="DIRTY",
        )
        request = MergeSweepRequest(prs=[pr], skip_polish=True)
        result = handler.handle(request)

        assert len(result.track_b_polish) == 0
        assert len(result.skipped) == 1


@pytest.mark.unit
class TestFailureHistory:
    """Cross-run failure history tracking and escalation."""

    async def test_chronic_pr_skips_polish(self, event_bus: EventBusInmemory) -> None:
        """A PR with >=5 consecutive failures should skip polish."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=99,
            title="fix: always broken",
            repo="OmniNode-ai/test",
            mergeable="CONFLICTING",
            merge_state_status="DIRTY",
        )
        history = {
            "OmniNode-ai/test#99": FailureHistoryEntry(
                first_seen="2026-04-01T00:00:00Z",
                last_seen="2026-04-05T00:00:00Z",
                consecutive_failures=5,
                total_failures=5,
                total_polishes=3,
                last_failure_categories=["conflict"],
                last_result="blocked",
                last_run_id="ms-prev",
                runs_seen=["ms-1", "ms-2", "ms-3", "ms-4", "ms-prev"],
            ),
        }
        request = MergeSweepRequest(prs=[pr], failure_history=history)
        result = handler.handle(request)

        assert len(result.track_b_polish) == 0
        assert len(result.skipped) == 1
        assert "CHRONIC" in result.skipped[0].reason

    async def test_recidivist_pr_skips_polish(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A PR polished >=3 times but still failing should skip polish."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=42,
            title="fix: keeps breaking",
            repo="OmniNode-ai/test",
            mergeable="MERGEABLE",
            merge_state_status="BLOCKED",
            required_checks_pass=False,
        )
        history = {
            "OmniNode-ai/test#42": FailureHistoryEntry(
                first_seen="2026-04-01T00:00:00Z",
                last_seen="2026-04-05T00:00:00Z",
                consecutive_failures=1,
                total_failures=4,
                total_polishes=3,
                last_failure_categories=["ci_test"],
                last_result="polished_and_queued",
                last_run_id="ms-prev",
                runs_seen=["ms-1", "ms-2", "ms-3", "ms-prev"],
            ),
        }
        request = MergeSweepRequest(prs=[pr], failure_history=history)
        result = handler.handle(request)

        assert len(result.track_b_polish) == 0
        assert len(result.skipped) == 1
        assert "RECIDIVIST" in result.skipped[0].reason

    async def test_stuck_pr_still_gets_polished(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A PR with 3-4 consecutive failures (STUCK) should still get polished."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=10,
            title="fix: struggling",
            repo="OmniNode-ai/test",
            mergeable="CONFLICTING",
            merge_state_status="DIRTY",
        )
        history = {
            "OmniNode-ai/test#10": FailureHistoryEntry(
                first_seen="2026-04-03T00:00:00Z",
                last_seen="2026-04-05T00:00:00Z",
                consecutive_failures=3,
                total_failures=3,
                total_polishes=1,
                last_failure_categories=["conflict"],
                last_result="blocked",
                last_run_id="ms-prev",
                runs_seen=["ms-1", "ms-2", "ms-prev"],
            ),
        }
        request = MergeSweepRequest(prs=[pr], failure_history=history)
        result = handler.handle(request)

        # STUCK but still below CHRONIC threshold — should still polish
        assert len(result.track_b_polish) == 1

    async def test_failure_history_summary_computed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Failure history summary should aggregate stats correctly."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=1,
            title="test",
            repo="test/repo",
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            review_decision="APPROVED",
            required_checks_pass=True,
        )
        history = {
            "test/repo#10": FailureHistoryEntry(
                first_seen="2026-04-01T00:00:00Z",
                last_seen="2026-04-05T00:00:00Z",
                consecutive_failures=3,
                total_failures=3,
            ),
            "test/repo#20": FailureHistoryEntry(
                first_seen="2026-04-01T00:00:00Z",
                last_seen="2026-04-05T00:00:00Z",
                consecutive_failures=6,
                total_failures=6,
            ),
            "test/repo#30": FailureHistoryEntry(
                first_seen="2026-04-01T00:00:00Z",
                last_seen="2026-04-05T00:00:00Z",
                consecutive_failures=1,
                total_failures=4,
                total_polishes=3,
            ),
        }
        request = MergeSweepRequest(prs=[pr], failure_history=history)
        result = handler.handle(request)

        assert result.failure_history_summary.total_tracked == 3
        assert result.failure_history_summary.stuck_prs == 1  # #10 (3 consecutive)
        assert result.failure_history_summary.chronic_prs == 1  # #20 (6 consecutive)
        assert (
            result.failure_history_summary.recidivist_prs == 1
        )  # #30 (3 polishes, still failing)

    async def test_no_failure_history_still_works(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Handler works correctly with empty failure history."""
        handler = NodeMergeSweep()
        pr = PRInfo(
            number=1,
            title="feat: new",
            repo="test/repo",
            mergeable="CONFLICTING",
            merge_state_status="DIRTY",
        )
        request = MergeSweepRequest(prs=[pr])
        result = handler.handle(request)

        assert len(result.track_b_polish) == 1
        assert result.failure_history_summary.total_tracked == 0


@pytest.mark.unit
class TestMergeSweepEventBus:
    """Event bus wiring tests."""

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus for command/completion flow."""
        handler = NodeMergeSweep()
        completions: list[dict[str, object]] = []

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)
            prs = [PRInfo(**pr_data) for pr_data in payload.get("prs", [])]
            request = MergeSweepRequest(prs=prs)
            result = handler.handle(request)
            completion = {
                "status": result.status,
                "track_a": len(result.track_a_merge),
                "track_b": len(result.track_b_polish),
            }
            completions.append(completion)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(completion).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-merge"
        )

        cmd_payload = json.dumps(
            {
                "prs": [
                    {
                        "number": 1,
                        "title": "test",
                        "repo": "test/repo",
                        "mergeable": "MERGEABLE",
                        "merge_state_status": "CLEAN",
                        "review_decision": "APPROVED",
                        "required_checks_pass": True,
                    }
                ]
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(completions) == 1
        assert completions[0]["status"] == "queued"
        assert completions[0]["track_a"] == 1

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()


@pytest.mark.unit
class TestLifecycleOrdering:
    """Lifecycle-ordered Track A merge sequencing via pr_lifecycle_orchestrator."""

    async def test_use_lifecycle_ordering_flag_accepted(
        self, event_bus: EventBusInmemory
    ) -> None:
        """use_lifecycle_ordering=True is accepted by the request model."""
        pr = PRInfo(
            number=1,
            title="feat: single PR",
            repo="OmniNode-ai/omnimarket",
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            review_decision="APPROVED",
            required_checks_pass=True,
        )
        request = MergeSweepRequest(prs=[pr], use_lifecycle_ordering=True)
        handler = NodeMergeSweep()
        result = handler.handle(request)

        assert result.status == "queued"
        assert len(result.track_a_merge) == 1

    async def test_lifecycle_ordering_preserves_all_track_a(
        self, event_bus: EventBusInmemory
    ) -> None:
        """All Track A PRs survive lifecycle ordering — none are dropped."""
        prs = [
            PRInfo(
                number=i,
                title=f"feat: PR {i}",
                repo="OmniNode-ai/omnimarket",
                mergeable="MERGEABLE",
                merge_state_status="CLEAN",
                review_decision="APPROVED",
                required_checks_pass=True,
            )
            for i in range(1, 5)
        ]
        request = MergeSweepRequest(prs=prs, use_lifecycle_ordering=True)
        handler = NodeMergeSweep()
        result = handler.handle(request)

        assert result.status == "queued"
        assert len(result.track_a_merge) == 4
        # All original PR numbers present
        numbers = {c.pr.number for c in result.track_a_merge}
        assert numbers == {1, 2, 3, 4}

    async def test_lifecycle_ordering_mixed_tracks(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Non-Track-A PRs are not reordered; Track A PRs are lifecycle-ordered."""
        prs = [
            PRInfo(
                number=10,
                title="feat: green PR",
                repo="OmniNode-ai/omniclaude",
                mergeable="MERGEABLE",
                merge_state_status="CLEAN",
                review_decision="APPROVED",
                required_checks_pass=True,
            ),
            PRInfo(
                number=20,
                title="fix: conflicted",
                repo="OmniNode-ai/omniclaude",
                mergeable="CONFLICTING",
                merge_state_status="DIRTY",
            ),
            PRInfo(
                number=30,
                title="feat: another green",
                repo="OmniNode-ai/omnimarket",
                mergeable="MERGEABLE",
                merge_state_status="CLEAN",
                review_decision="APPROVED",
                required_checks_pass=True,
            ),
        ]
        request = MergeSweepRequest(prs=prs, use_lifecycle_ordering=True)
        handler = NodeMergeSweep()
        result = handler.handle(request)

        assert result.status == "queued"
        assert len(result.track_a_merge) == 2
        assert len(result.track_b_polish) == 1
        # Track A PRs contain both green PRs
        track_a_numbers = {c.pr.number for c in result.track_a_merge}
        assert track_a_numbers == {10, 30}

    async def test_lifecycle_ordering_false_by_default(
        self, event_bus: EventBusInmemory
    ) -> None:
        """use_lifecycle_ordering defaults to False — existing behaviour unchanged."""
        pr = PRInfo(
            number=1,
            title="feat: test",
            repo="OmniNode-ai/omnimarket",
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            review_decision="APPROVED",
            required_checks_pass=True,
        )
        request = MergeSweepRequest(prs=[pr])
        assert request.use_lifecycle_ordering is False
        handler = NodeMergeSweep()
        result = handler.handle(request)
        assert result.status == "queued"

    async def test_lifecycle_ordering_empty_track_a_no_error(
        self, event_bus: EventBusInmemory
    ) -> None:
        """use_lifecycle_ordering=True with no Track A PRs does not error."""
        pr = PRInfo(
            number=99,
            title="wip: draft",
            repo="OmniNode-ai/omnimarket",
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            is_draft=True,
        )
        request = MergeSweepRequest(prs=[pr], use_lifecycle_ordering=True)
        handler = NodeMergeSweep()
        result = handler.handle(request)

        assert result.status == "nothing_to_merge"
        assert len(result.track_a_merge) == 0

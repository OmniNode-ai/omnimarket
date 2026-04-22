"""Golden chain tests for node_merge_sweep.

The handler (NodeMergeSweep) is pure compute — all classification logic
is table-driven with zero network calls. Tests construct ModelPRInfo directly
and verify track assignment.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    EnumPRTrack,
    ModelFailureHistoryEntry,
    ModelMergeSweepRequest,
    ModelPRInfo,
    NodeMergeSweep,
)


def _pr(**overrides: object) -> ModelPRInfo:
    defaults: dict[str, object] = {
        "number": 1,
        "title": "test PR",
        "repo": "OmniNode-ai/omnimarket",
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "is_draft": False,
        "review_decision": "APPROVED",
        "required_checks_pass": True,
        "labels": [],
        "required_approving_review_count": 1,
    }
    defaults.update(overrides)
    return ModelPRInfo(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
class TestMergeSweepClassification:
    """Core track classification logic."""

    def test_draft_skipped(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(ModelMergeSweepRequest(prs=[_pr(is_draft=True)]))
        assert result.classified[0].track == EnumPRTrack.SKIP
        assert "Draft" in result.classified[0].reason

    def test_merge_ready(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(ModelMergeSweepRequest(prs=[_pr()]))
        assert result.classified[0].track == EnumPRTrack.A_MERGE
        assert result.classified[0].reason == "Merge-ready"

    def test_behind_goes_to_a_update(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(
            ModelMergeSweepRequest(prs=[_pr(merge_state_status="BEHIND")])
        )
        assert result.classified[0].track == EnumPRTrack.A_UPDATE
        assert "stale" in result.classified[0].reason.lower()

    def test_unknown_mergeable_goes_to_a_update(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(ModelMergeSweepRequest(prs=[_pr(mergeable="UNKNOWN")]))
        assert result.classified[0].track == EnumPRTrack.A_UPDATE

    def test_conflicting_goes_to_b_polish(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(
            ModelMergeSweepRequest(prs=[_pr(mergeable="CONFLICTING")])
        )
        assert result.classified[0].track == EnumPRTrack.B_POLISH
        assert "conflicts" in result.classified[0].reason.lower()

    def test_ci_failing_goes_to_b_polish(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(
            ModelMergeSweepRequest(prs=[_pr(required_checks_pass=False)])
        )
        assert result.classified[0].track == EnumPRTrack.B_POLISH
        assert "CI" in result.classified[0].reason

    def test_changes_requested_goes_to_b_polish(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(
            ModelMergeSweepRequest(prs=[_pr(review_decision="CHANGES_REQUESTED")])
        )
        assert result.classified[0].track == EnumPRTrack.B_POLISH

    def test_blocked_by_threads_goes_to_a_resolve(self) -> None:
        """MERGEABLE + BLOCKED + checks pass = thread resolution."""
        handler = NodeMergeSweep()
        result = handler.handle(
            ModelMergeSweepRequest(
                prs=[_pr(merge_state_status="BLOCKED", required_checks_pass=True)]
            )
        )
        assert result.classified[0].track == EnumPRTrack.A_RESOLVE
        assert "thread" in result.classified[0].reason.lower()

    def test_review_bot_gate_failed_goes_to_a_resolve(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(
            ModelMergeSweepRequest(prs=[_pr(review_bot_gate_passed=False)])
        )
        assert result.classified[0].track == EnumPRTrack.A_RESOLVE

    def test_review_bot_gate_failed_not_merge_ready(self) -> None:
        """review_bot_gate_passed=False should NOT be Track A even if all else green."""
        handler = NodeMergeSweep()
        result = handler.handle(
            ModelMergeSweepRequest(prs=[_pr(review_bot_gate_passed=False)])
        )
        assert result.classified[0].track != EnumPRTrack.A_MERGE

    def test_no_approval_required_solo_dev(self) -> None:
        """required_approving_review_count=0 should allow merge without APPROVED."""
        handler = NodeMergeSweep()
        result = handler.handle(
            ModelMergeSweepRequest(
                prs=[_pr(review_decision=None, required_approving_review_count=0)]
            )
        )
        assert result.classified[0].track == EnumPRTrack.A_MERGE

    def test_no_approval_required_none(self) -> None:
        """required_approving_review_count=None (no protection) allows merge."""
        handler = NodeMergeSweep()
        result = handler.handle(
            ModelMergeSweepRequest(
                prs=[_pr(review_decision=None, required_approving_review_count=None)]
            )
        )
        assert result.classified[0].track == EnumPRTrack.A_MERGE


@pytest.mark.unit
class TestMergeSweepResultProperties:
    """Result track accessors."""

    def test_track_accessors(self) -> None:
        handler = NodeMergeSweep()
        prs = [
            _pr(number=1),  # A_MERGE
            _pr(number=2, is_draft=True),  # SKIP
            _pr(number=3, mergeable="CONFLICTING"),  # B_POLISH
            _pr(number=4, merge_state_status="BEHIND"),  # A_UPDATE
            _pr(
                number=5, merge_state_status="BLOCKED", required_checks_pass=True
            ),  # A_RESOLVE
        ]
        result = handler.handle(ModelMergeSweepRequest(prs=prs))

        assert len(result.track_a_merge) == 1
        assert len(result.track_a_update) == 1
        assert len(result.track_a_resolve) == 1
        assert len(result.track_b_polish) == 1
        assert len(result.skipped) == 1

    def test_status_queued_when_actionable(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(ModelMergeSweepRequest(prs=[_pr()]))
        assert result.status == "queued"

    def test_status_nothing_when_empty(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(ModelMergeSweepRequest(prs=[]))
        assert result.status == "nothing_to_merge"


@pytest.mark.unit
class TestMergeSweepFailureHistory:
    """Failure history escalation."""

    def test_chronic_skips_polish(self) -> None:
        handler = NodeMergeSweep()
        history = {
            "OmniNode-ai/omnimarket#3": ModelFailureHistoryEntry(
                first_seen="2026-01-01",
                last_seen="2026-01-05",
                consecutive_failures=5,
                total_failures=10,
            )
        }
        result = handler.handle(
            ModelMergeSweepRequest(
                prs=[_pr(number=3, mergeable="CONFLICTING")],
                failure_history=history,
            )
        )
        assert result.classified[0].track == EnumPRTrack.SKIP
        assert "CHRONIC" in result.classified[0].reason

    def test_recidivist_skips_polish(self) -> None:
        handler = NodeMergeSweep()
        history = {
            "OmniNode-ai/omnimarket#3": ModelFailureHistoryEntry(
                first_seen="2026-01-01",
                last_seen="2026-01-05",
                consecutive_failures=1,
                total_failures=3,
                total_polishes=3,
            )
        }
        result = handler.handle(
            ModelMergeSweepRequest(
                prs=[_pr(number=3, mergeable="CONFLICTING")],
                failure_history=history,
            )
        )
        assert result.classified[0].track == EnumPRTrack.SKIP
        assert "RECIDIVIST" in result.classified[0].reason

    def test_failure_summary(self) -> None:
        handler = NodeMergeSweep()
        history = {
            "a#1": ModelFailureHistoryEntry(
                first_seen="2026-01-01", last_seen="2026-01-02", consecutive_failures=3
            ),
            "a#2": ModelFailureHistoryEntry(
                first_seen="2026-01-01", last_seen="2026-01-05", consecutive_failures=5
            ),
        }
        result = handler.handle(ModelMergeSweepRequest(prs=[], failure_history=history))
        assert result.failure_history_summary.total_tracked == 2
        assert result.failure_history_summary.stuck_prs == 1
        assert result.failure_history_summary.chronic_prs == 1


@pytest.mark.unit
class TestMergeSweepCaps:
    """max_total_merges and skip_polish."""

    def test_max_total_merges_cap(self) -> None:
        handler = NodeMergeSweep()
        prs = [_pr(number=i) for i in range(5)]
        result = handler.handle(ModelMergeSweepRequest(prs=prs, max_total_merges=2))
        assert len(result.track_a_merge) == 2
        assert len(result.skipped) == 3

    def test_skip_polish(self) -> None:
        handler = NodeMergeSweep()
        result = handler.handle(
            ModelMergeSweepRequest(
                prs=[_pr(number=1, mergeable="CONFLICTING")],
                skip_polish=True,
            )
        )
        assert result.classified[0].track == EnumPRTrack.SKIP
        assert "Polish skipped" in result.classified[0].reason

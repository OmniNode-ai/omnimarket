# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for merge_sweep review-bot/all-findings-resolved gate — OMN-8492, Component 4."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
    ModelMergeSweepRequest,
    ModelPRInfo,
    NodeMergeSweep,
)


def _base_pr(**kwargs: object) -> ModelPRInfo:
    defaults = {
        "number": 1,
        "title": "feat: test",
        "repo": "OmniNode-ai/omnimarket",
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "review_decision": "APPROVED",
        "required_checks_pass": True,
    }
    defaults.update(kwargs)
    return ModelPRInfo(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
class TestReviewBotGateMergeSweep:
    def test_review_bot_gate_passed_true_is_merge_ready(self) -> None:
        """A PR with review_bot_gate_passed=True should be Track A (merge-ready)."""
        handler = NodeMergeSweep()
        pr = _base_pr(
            merge_state_status="CLEAN",
            review_bot_gate_passed=True,
        )
        result = handler.handle(ModelMergeSweepRequest(prs=[pr]))
        assert len(result.track_a_merge) == 1

    def test_review_bot_gate_failed_goes_to_track_a_resolve(self) -> None:
        """A PR with review_bot_gate_passed=False should go to Track A-resolve."""
        handler = NodeMergeSweep()
        pr = _base_pr(
            mergeable="MERGEABLE",
            merge_state_status="CLEAN",
            required_checks_pass=True,
            review_bot_gate_passed=False,
        )
        result = handler.handle(ModelMergeSweepRequest(prs=[pr]))
        assert len(result.track_a_resolve) == 1
        assert result.track_a_resolve[0].pr.number == 1

    def test_review_bot_gate_none_with_clean_status_is_merge_ready(self) -> None:
        """review_bot_gate_passed=None on a CLEAN PR means gate not yet set — still merge-ready."""
        handler = NodeMergeSweep()
        pr = _base_pr(
            merge_state_status="CLEAN",
            review_bot_gate_passed=None,
        )
        result = handler.handle(ModelMergeSweepRequest(prs=[pr]))
        assert len(result.track_a_merge) == 1

    def test_review_bot_gate_none_with_blocked_status_goes_to_track_a_resolve(
        self,
    ) -> None:
        """Legacy path: MERGEABLE+BLOCKED+green checks = waiting for thread resolution."""
        handler = NodeMergeSweep()
        pr = _base_pr(
            merge_state_status="BLOCKED",
            required_checks_pass=True,
            review_bot_gate_passed=None,
        )
        result = handler.handle(ModelMergeSweepRequest(prs=[pr]))
        assert len(result.track_a_resolve) == 1

    def test_review_bot_gate_passed_with_blocked_status_is_merge_ready(self) -> None:
        """Gate=True means bot has signed off; BLOCKED status should not prevent merge."""
        handler = NodeMergeSweep()
        pr = _base_pr(
            merge_state_status="CLEAN",
            required_checks_pass=True,
            review_bot_gate_passed=True,
        )
        result = handler.handle(ModelMergeSweepRequest(prs=[pr]))
        assert len(result.track_a_merge) == 1

    def test_draft_pr_is_skipped_regardless_of_gate(self) -> None:
        handler = NodeMergeSweep()
        pr = _base_pr(
            is_draft=True,
            review_bot_gate_passed=True,
        )
        result = handler.handle(ModelMergeSweepRequest(prs=[pr]))
        assert len(result.skipped) == 1

    def test_multiple_prs_gate_mix(self) -> None:
        """Mix of gate states: one passes, one fails, one legacy."""
        handler = NodeMergeSweep()
        prs = [
            _base_pr(number=1, merge_state_status="CLEAN", review_bot_gate_passed=True),
            _base_pr(
                number=2,
                merge_state_status="CLEAN",
                required_checks_pass=True,
                review_bot_gate_passed=False,
            ),
            _base_pr(
                number=3,
                merge_state_status="BLOCKED",
                required_checks_pass=True,
                review_bot_gate_passed=None,
            ),
        ]
        result = handler.handle(ModelMergeSweepRequest(prs=prs))
        assert len(result.track_a_merge) == 1
        assert result.track_a_merge[0].pr.number == 1
        assert len(result.track_a_resolve) == 2
        resolve_nums = {c.pr.number for c in result.track_a_resolve}
        assert resolve_nums == {2, 3}

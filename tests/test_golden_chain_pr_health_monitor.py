# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_pr_health_monitor.

Verifies classification logic, multi-flag detection, staleness thresholds,
severity ordering, aggregate summary accuracy, and draft exclusion.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import ModelPRInfo
from omnimarket.nodes.node_pr_health_monitor.handlers.handler_pr_health_monitor import (
    HandlerPrHealthMonitor,
    _classify_pr,
)
from omnimarket.nodes.node_pr_health_monitor.models.model_pr_health_input import (
    ModelPrHealthInput,
)
from omnimarket.nodes.node_pr_health_monitor.models.model_pr_health_result import (
    EnumPrHealthStatus,
    ModelPrHealthReport,
)


def _pr(
    number: int = 1,
    repo: str = "OmniNode-ai/omniclaude",
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    is_draft: bool = False,
    review_decision: str | None = "APPROVED",
    required_checks_pass: bool = True,
) -> ModelPRInfo:
    return ModelPRInfo(
        number=number,
        title=f"PR #{number}",
        repo=repo,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        is_draft=is_draft,
        review_decision=review_decision,
        required_checks_pass=required_checks_pass,
    )


@pytest.mark.unit
class TestClassifyPrUnit:
    """Unit tests for _classify_pr helper."""

    def test_healthy_pr(self) -> None:
        status, reasons = _classify_pr(
            pr=_pr(), age_days=1, stale_inactive_threshold_days=3, has_age=True
        )
        assert status == EnumPrHealthStatus.HEALTHY
        assert reasons == ()

    def test_ci_failing_is_stale_red(self) -> None:
        status, reasons = _classify_pr(
            pr=_pr(required_checks_pass=False),
            age_days=0,
            stale_inactive_threshold_days=3,
            has_age=False,
        )
        assert status == EnumPrHealthStatus.STALE_RED
        assert "CI failing" in reasons

    def test_conflicting_is_conflicted(self) -> None:
        status, reasons = _classify_pr(
            pr=_pr(mergeable="CONFLICTING", merge_state_status="DIRTY"),
            age_days=0,
            stale_inactive_threshold_days=3,
            has_age=False,
        )
        assert status == EnumPrHealthStatus.CONFLICTED
        assert "merge conflict" in reasons

    def test_changes_requested_is_review_blocked(self) -> None:
        status, reasons = _classify_pr(
            pr=_pr(review_decision="CHANGES_REQUESTED"),
            age_days=0,
            stale_inactive_threshold_days=3,
            has_age=False,
        )
        assert status == EnumPrHealthStatus.REVIEW_BLOCKED
        assert "changes requested" in reasons

    def test_stale_inactive_when_age_known(self) -> None:
        status, reasons = _classify_pr(
            pr=_pr(),
            age_days=5,
            stale_inactive_threshold_days=3,
            has_age=True,
        )
        assert status == EnumPrHealthStatus.STALE_INACTIVE
        assert "no activity for 5d" in reasons

    def test_stale_inactive_skipped_when_age_unknown(self) -> None:
        """Without age data, STALE_INACTIVE should not be flagged."""
        status, _ = _classify_pr(
            pr=_pr(),
            age_days=0,
            stale_inactive_threshold_days=3,
            has_age=False,
        )
        assert status == EnumPrHealthStatus.HEALTHY

    def test_draft_is_always_healthy(self) -> None:
        status, reasons = _classify_pr(
            pr=_pr(is_draft=True, required_checks_pass=False, mergeable="CONFLICTING"),
            age_days=10,
            stale_inactive_threshold_days=3,
            has_age=True,
        )
        assert status == EnumPrHealthStatus.HEALTHY
        assert reasons == ()

    def test_ci_failing_beats_conflict_in_severity(self) -> None:
        """When both CI failing and conflicting, STALE_RED wins (higher severity)."""
        status, reasons = _classify_pr(
            pr=_pr(required_checks_pass=False, mergeable="CONFLICTING"),
            age_days=0,
            stale_inactive_threshold_days=3,
            has_age=False,
        )
        assert status == EnumPrHealthStatus.STALE_RED
        assert "CI failing" in reasons
        assert "merge conflict" in reasons

    def test_exactly_at_stale_threshold_is_flagged(self) -> None:
        status, _ = _classify_pr(
            pr=_pr(),
            age_days=3,
            stale_inactive_threshold_days=3,
            has_age=True,
        )
        assert status == EnumPrHealthStatus.STALE_INACTIVE

    def test_one_below_stale_threshold_is_healthy(self) -> None:
        status, _ = _classify_pr(
            pr=_pr(),
            age_days=2,
            stale_inactive_threshold_days=3,
            has_age=True,
        )
        assert status == EnumPrHealthStatus.HEALTHY


@pytest.mark.unit
class TestHandlerPrHealthMonitorGoldenChain:
    """Golden chain: input model -> handler -> ModelPrHealthReport."""

    def test_empty_input_produces_empty_report(self) -> None:
        handler = HandlerPrHealthMonitor()
        result = handler.handle(ModelPrHealthInput(prs=()))

        assert isinstance(result, ModelPrHealthReport)
        assert result.summary.total == 0
        assert result.entries == ()
        assert result.flagged == []

    def test_single_healthy_pr(self) -> None:
        handler = HandlerPrHealthMonitor()
        result = handler.handle(
            ModelPrHealthInput(
                prs=(_pr(number=1),),
                pr_age_days={"OmniNode-ai/omniclaude#1": 1},
            )
        )

        assert result.summary.total == 1
        assert result.summary.healthy == 1
        assert result.flagged == []

    def test_stale_red_pr_is_flagged(self) -> None:
        handler = HandlerPrHealthMonitor()
        result = handler.handle(
            ModelPrHealthInput(
                prs=(_pr(number=10, required_checks_pass=False),),
            )
        )

        assert result.summary.stale_red == 1
        assert len(result.flagged) == 1
        assert result.flagged[0].status == EnumPrHealthStatus.STALE_RED

    def test_conflicted_pr_is_flagged(self) -> None:
        handler = HandlerPrHealthMonitor()
        result = handler.handle(
            ModelPrHealthInput(
                prs=(
                    _pr(number=20, mergeable="CONFLICTING", merge_state_status="DIRTY"),
                ),
            )
        )

        assert result.summary.conflicted == 1
        assert result.flagged[0].status == EnumPrHealthStatus.CONFLICTED

    def test_review_blocked_pr(self) -> None:
        handler = HandlerPrHealthMonitor()
        result = handler.handle(
            ModelPrHealthInput(
                prs=(_pr(number=30, review_decision="CHANGES_REQUESTED"),),
            )
        )

        assert result.summary.review_blocked == 1

    def test_stale_inactive_with_age_map(self) -> None:
        handler = HandlerPrHealthMonitor()
        result = handler.handle(
            ModelPrHealthInput(
                prs=(_pr(number=40),),
                pr_age_days={"OmniNode-ai/omniclaude#40": 5},
                stale_inactive_threshold_days=3,
            )
        )

        assert result.summary.stale_inactive == 1
        assert result.entries[0].age_days == 5

    def test_mixed_prs_summary(self) -> None:
        """Multiple PR types — verify summary counts are correct."""
        handler = HandlerPrHealthMonitor()
        prs = (
            _pr(number=1),  # healthy
            _pr(number=2, required_checks_pass=False),  # stale_red
            _pr(
                number=3, mergeable="CONFLICTING", merge_state_status="DIRTY"
            ),  # conflicted
            _pr(number=4, review_decision="CHANGES_REQUESTED"),  # review_blocked
            _pr(number=5, is_draft=True),  # healthy (draft)
        )
        result = handler.handle(ModelPrHealthInput(prs=prs))

        assert result.summary.total == 5
        assert result.summary.healthy == 2  # PR 1 + draft PR 5
        assert result.summary.stale_red == 1
        assert result.summary.conflicted == 1
        assert result.summary.review_blocked == 1
        assert len(result.flagged) == 3

    def test_flagged_ordered_by_severity(self) -> None:
        """flagged list must be sorted: stale_red before conflicted before stale_inactive."""
        handler = HandlerPrHealthMonitor()
        prs = (
            _pr(number=1),  # healthy — should not appear in flagged
            _pr(
                number=2, mergeable="CONFLICTING", merge_state_status="DIRTY"
            ),  # conflicted
            _pr(number=3, required_checks_pass=False),  # stale_red
        )
        result = handler.handle(ModelPrHealthInput(prs=prs))

        assert len(result.flagged) == 2
        assert result.flagged[0].status == EnumPrHealthStatus.STALE_RED
        assert result.flagged[1].status == EnumPrHealthStatus.CONFLICTED

    def test_generated_at_is_iso8601(self) -> None:
        handler = HandlerPrHealthMonitor()
        result = handler.handle(ModelPrHealthInput(prs=()))
        assert "T" in result.generated_at
        assert result.generated_at.endswith("Z")

    def test_handler_type_and_category(self) -> None:
        handler = HandlerPrHealthMonitor()
        assert handler.handler_type == "NODE_HANDLER"
        assert handler.handler_category == "COMPUTE"

    def test_age_map_key_format(self) -> None:
        """Age map keys must be '{repo}#{number}' for correct lookup."""
        handler = HandlerPrHealthMonitor()
        pr = _pr(number=99, repo="OmniNode-ai/omnibase_core")
        result = handler.handle(
            ModelPrHealthInput(
                prs=(pr,),
                pr_age_days={"OmniNode-ai/omnibase_core#99": 10},
                stale_inactive_threshold_days=3,
            )
        )

        assert result.entries[0].age_days == 10
        assert result.entries[0].status == EnumPrHealthStatus.STALE_INACTIVE

    def test_custom_thresholds(self) -> None:
        """Custom thresholds are respected."""
        handler = HandlerPrHealthMonitor()
        pr = _pr(number=1)
        result = handler.handle(
            ModelPrHealthInput(
                prs=(pr,),
                pr_age_days={"OmniNode-ai/omniclaude#1": 7},
                stale_inactive_threshold_days=10,  # higher threshold — should be healthy
            )
        )
        assert result.summary.healthy == 1
        assert result.summary.stale_inactive == 0

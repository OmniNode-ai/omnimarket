# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13541: code_review is a first-class delegation task class.

Before this fix, `code_review` was accepted at the consumer-facing delegation
surface (node_delegate_skill_orchestrator.allowed_task_types + the claude/codex
adapter) but had no task-class contract entry and only the single local-coder
model in routing_tiers `use_for`. A code_review delegation that reached the
quality gate raised RequiredBarAuthorityError (no task class) and, when the local
backend was unavailable, stranded with no cloud fallback — a Pattern-B timeout
with zero inference.

These tests pin the omnimarket-side wiring against the REAL committed configs
(task_class_contracts.v1.yaml + routing_tiers.yaml), independent of the
omnibase_core wire DTO pin.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    resolve_required_bar_authority,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_config,
    _get_task_class_contract,
    _task_class_entry,
)


@pytest.mark.unit
class TestCodeReviewTaskClass:
    def test_task_class_contract_declares_code_review(self) -> None:
        """code_review has a task-class entry (mirrors review, code-review DoD)."""
        contract = _get_task_class_contract()
        entry = _task_class_entry(contract, "code_review")
        assert entry is not None, (
            "code_review must be declared in task_class_contracts.v1.yaml so the "
            "quality gate's required-bar authority can resolve it"
        )

    def test_required_bar_authority_resolves_for_code_review(self) -> None:
        """resolve_required_bar_authority no longer raises for code_review.

        Before OMN-13541 this raised RequiredBarAuthorityError (task class not
        declared), which terminalized the workflow as FAILED with no inference.
        """
        authority = resolve_required_bar_authority(task_type="code_review")
        assert 0.0 <= authority.required_bar <= 1.0
        assert authority.authority_source == "task_class:code_review"

    def test_routing_tiers_serve_code_review_with_cloud_fallback(self) -> None:
        """code_review is routable on BOTH a local tier and a cheap_cloud tier.

        The single-local-backend topology was the strand risk; the cheap_cloud
        glm-5.2 fallback (escalation_policy.tier_order [local, cheap_cloud])
        removes the single point of failure.
        """
        config = _get_config()
        tiers_serving = {
            tier.name
            for tier in config.tiers
            for model in tier.models
            if "code_review" in model.use_for
        }
        assert "local" in tiers_serving, (
            f"local tier must serve code_review; got {tiers_serving}"
        )
        assert "cheap_cloud" in tiers_serving, (
            "cheap_cloud must serve code_review so escalation has a cloud fallback; "
            f"got {tiers_serving}"
        )

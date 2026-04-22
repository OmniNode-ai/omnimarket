"""Golden chain tests for node_fixer_dispatcher.

Pure compute — all routing is table-driven with zero network calls.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_fixer_dispatcher.handlers.handler_fixer_dispatcher import (
    HandlerFixerDispatcher,
)
from omnimarket.nodes.node_fixer_dispatcher.models.model_fixer_dispatch import (
    ModelFixerDispatchRequest,
)


def _make_request(**overrides: object) -> ModelFixerDispatchRequest:
    defaults = {
        "pr_number": 100,
        "repo": "omnimarket",
        "stall_category": "red",
        "blocking_reason": "CI failing",
        "stall_count": 2,
    }
    defaults.update(overrides)
    return ModelFixerDispatchRequest(**defaults)  # type: ignore[arg-type]


@pytest.mark.unit
class TestFixerDispatcherGoldenChain:
    def test_red_routes_to_ci_fix(self) -> None:
        handler = HandlerFixerDispatcher()
        result = handler.handle(_make_request(stall_category="red"))

        assert result.action == "dispatch_ci_fix"
        assert result.target_node == "node_ci_fix_effect"
        assert "ci-fix" in result.target_topic
        assert result.confidence >= 0.9

    def test_conflicted_routes_to_conflict_hunk(self) -> None:
        handler = HandlerFixerDispatcher()
        result = handler.handle(_make_request(stall_category="conflicted"))

        assert result.action == "dispatch_conflict_resolve"
        assert result.target_node == "node_conflict_hunk_effect"
        assert result.confidence >= 0.9

    def test_behind_routes_to_rebase(self) -> None:
        handler = HandlerFixerDispatcher()
        result = handler.handle(
            _make_request(stall_category="behind", branch_name="jonah/omn-9403-foo")
        )

        assert result.action == "dispatch_rebase"
        assert result.target_node == "node_rebase_effect"
        assert result.payload_hint.get("branch_name") == "jonah/omn-9403-foo"

    def test_deploy_gate_returns_advisory(self) -> None:
        handler = HandlerFixerDispatcher()
        result = handler.handle(
            _make_request(
                stall_category="deploy_gate", blocking_reason="deploy-agent RED"
            )
        )

        assert result.action == "dispatch_deploy_gate_skip"
        assert result.target_node == ""
        assert "skip-deploy-gate" in result.reason

    def test_unknown_esculates(self) -> None:
        handler = HandlerFixerDispatcher()
        result = handler.handle(_make_request(stall_category="unknown"))

        assert result.action == "escalate"
        assert result.target_node == ""
        assert result.confidence == 0.0

    def test_stale_esculates(self) -> None:
        handler = HandlerFixerDispatcher()
        result = handler.handle(_make_request(stall_category="stale", stall_count=10))

        assert result.action == "escalate"
        assert "stall_count=10" in result.reason

    def test_payload_hint_includes_dry_run(self) -> None:
        handler = HandlerFixerDispatcher()
        result = handler.handle(_make_request(stall_category="red", dry_run=True))

        assert result.payload_hint.get("dry_run") == "true"

    def test_policy_blocks_dispatch(self) -> None:
        from unittest.mock import MagicMock

        policy = MagicMock()
        policy.should_dispatch.return_value = "release freeze active"

        handler = HandlerFixerDispatcher(policy=policy)
        result = handler.handle(_make_request(stall_category="red"))

        assert result.action == "escalate"
        assert "release freeze" in result.reason

    def test_policy_allows_dispatch(self) -> None:
        from unittest.mock import MagicMock

        policy = MagicMock()
        policy.should_dispatch.return_value = None

        handler = HandlerFixerDispatcher(policy=policy)
        result = handler.handle(_make_request(stall_category="conflicted"))

        assert result.action == "dispatch_conflict_resolve"

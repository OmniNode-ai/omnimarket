# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression coverage for all-node market runtime dogfood inventory."""

from __future__ import annotations

from scripts.audit.market_node_runtime_dogfood import build_report


def test_market_node_runtime_dogfood_inventory_classifies_all_entry_points() -> None:
    report = build_report()
    summary = report["summary"]

    assert summary["node_dirs"] == 290
    assert summary["entry_points"] == 290
    assert summary["missing_entry_points"] == []
    assert summary["dangling_entry_points"] == []
    assert summary["routable"] >= 235
    assert summary["failed"] <= 55
    assert summary["failure_buckets"] == {
        "missing_handler_route": 3,
        "missing_input_model": 28,
        "non_command_or_missing_command_topic": 24,
    }


def test_market_node_runtime_dogfood_proves_nested_contract_shapes() -> None:
    report = build_report()
    routable = {item["node_name"]: item for item in report["routable"]}

    assert (
        routable["node_ab_compare_orchestrator"]["input_model"]
        == "omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_start.ModelAbCompareStart"
    )
    assert (
        routable["node_loop_state_reducer"]["handler"]
        == "omnimarket.nodes.node_loop_state_reducer.handlers.handler_loop_state.HandlerLoopState"
    )
    assert (
        routable["node_adr_canary_orchestrator"]["input_model"]
        == "omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request.ModelCanaryCommandPayload"
    )

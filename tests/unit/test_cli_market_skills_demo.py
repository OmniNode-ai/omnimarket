# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the market skill demo catalog CLI."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from omnimarket.cli.cli_market_skills_demo import main


@pytest.mark.unit
def test_cli_help_prints_demo_options() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    assert "--demo" in result.output
    assert "--no-run-smokes" in result.output
    assert "--include-pytest" in result.output
    assert "--output" in result.output


@pytest.mark.unit
def test_cli_lists_all_demo_lanes_without_running_smokes() -> None:
    result = CliRunner().invoke(main, ["--no-run-smokes"])

    assert result.exit_code == 0
    assert "MARKET SKILL DEMO CATALOG" in result.output
    assert "cost-routing-projection" in result.output
    assert "all-model-cost-arbitrage" in result.output
    assert "merge-delegation" in result.output
    assert "review-cost-control" in result.output
    assert "session-dispatch" in result.output
    assert "review-escalation-gate" in result.output
    assert "ticket-intake-routing" in result.output
    assert "pr-health-triage" in result.output
    assert "review-thread-policy-control" in result.output
    assert "node_model_router" in result.output
    assert "node_pr_lifecycle_orchestrator" in result.output
    assert "node_session_orchestrator" in result.output


@pytest.mark.unit
def test_cli_json_for_review_demo_contains_expected_nodes() -> None:
    result = CliRunner().invoke(
        main,
        [
            "--demo",
            "review-cost-control",
            "--no-run-smokes",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["run_smokes"] is False
    assert len(payload["demos"]) == 1
    demo = payload["demos"][0]
    assert demo["demo_id"] == "review-cost-control"
    assert demo["proof_status"] == "not-run"
    assert demo["market_nodes"] == [
        "node_local_review",
        "node_coderabbit_triage",
        "node_pr_polish",
    ]
    assert "cost-avoidance" in demo["value_tags"]


@pytest.mark.unit
def test_cli_json_for_all_model_cost_arbitrage_lists_ab_nodes() -> None:
    result = CliRunner().invoke(
        main,
        [
            "--demo",
            "all-model-cost-arbitrage",
            "--no-run-smokes",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    demo = payload["demos"][0]
    assert demo["demo_id"] == "all-model-cost-arbitrage"
    assert demo["proof_status"] == "external-command"
    assert demo["market_nodes"] == [
        "node_ab_compare_orchestrator",
        "node_ab_inference_effect",
        "node_ab_compare_reducer",
        "node_model_router",
    ]
    assert "cost-savings" in demo["value_tags"]
    assert "uv run ab-compare-suite --models all" in demo["command_hint"]


@pytest.mark.unit
def test_cli_json_for_review_thread_policy_control_lists_governance_nodes() -> None:
    result = CliRunner().invoke(
        main,
        [
            "--demo",
            "review-thread-policy-control",
            "--no-run-smokes",
            "--output",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    demo = payload["demos"][0]
    assert demo["demo_id"] == "review-thread-policy-control"
    assert demo["market_nodes"] == [
        "node_thread_reply_effect",
        "node_review_thread_reconciler",
        "node_finding_aggregator_compute",
    ]
    assert "governance" in demo["value_tags"]
    assert "test_handler_thread_reply.py" in demo["command_hint"]


@pytest.mark.unit
def test_cli_rejects_unknown_demo() -> None:
    result = CliRunner().invoke(main, ["--demo", "missing", "--no-run-smokes"])

    assert result.exit_code != 0
    assert "Unknown demo" in result.output
    assert "all-model-cost-arbitrage" in result.output
    assert "review-cost-control" in result.output

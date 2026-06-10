# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain contract coverage for node_pr_review_bot."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CONTRACT_PATH = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_review_bot"
    / "contract.yaml"
)


@pytest.mark.unit
def test_golden_chain_pr_review_bot_contract_preserves_runtime_surface() -> None:
    """The workflow contract must keep the live command, completion, and handler surface wired."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text())

    assert contract["node_type"] == "workflow"
    assert contract["descriptor"]["purity"] == "side_effect"
    assert contract["descriptor"]["runtime_profiles"] == ["effects"]

    handlers = {
        route["operation"]: route["handler"]["name"]
        for route in contract["handler_routing"]["handlers"]
    }
    assert handlers == {
        "fsm_transition": "HandlerPrReviewBot",
        "fetch_diff": "HandlerDiffFetcher",
        "post_threads": "HandlerThreadPoster",
        "watch_threads": "HandlerThreadWatcher",
        "judge_verify": "HandlerJudgeVerifier",
        "post_report": "HandlerReportPoster",
        "emergency_bypass": "HandlerEmergencyBypassParser",
        "verify_push": "HandlerVerificationLoop",
        "llm_review": "HandlerLlmReviewer",
        "commit_citation_verify": "HandlerCommitCitationVerifier",
        "webhook_reconcile": "HandlerWebhookReconciler",
    }

    assert contract["model_routing"]["reviewer"]["schema"] == (
        "required_caller_supplied_logical_keys"
    )
    assert contract["model_routing"]["judge"]["schema"] == (
        "required_caller_supplied_logical_key"
    )
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.pr-review-bot-completed.v1"
    )
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.pr-review-bot-start.v1",
        "onex.cmd.omnimarket.pr-review-bot-verify-push.v1",
    ]
    assert contract["event_bus"]["publish_topics"]

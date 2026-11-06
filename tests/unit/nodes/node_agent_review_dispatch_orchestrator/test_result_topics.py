# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract binds each result topic to the handler whose result it publishes."""

from __future__ import annotations

import typing
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_agent_review_dispatch_orchestrator.handlers.handler_agent_review_quorum import (
    HandlerAgentReviewQuorum,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.handlers.handler_pr_review_trigger import (
    HandlerPrReviewTrigger,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models import (
    ModelAgentQuorumResult,
)
from omnimarket.nodes.node_agent_review_dispatch_orchestrator.models.model_pr_review_trigger import (
    ModelPrReviewTriggerResult,
)

from .support import NODE

pytestmark = pytest.mark.unit

_NODE_DIR = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes" / NODE
_CONTRACT = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
_PUBLISHED = {e["event_type"]: e["topic"] for e in _CONTRACT["published_events"]}


def _return_type(handler: type) -> object:
    return typing.get_type_hints(handler.handle)["return"]


def test_the_trigger_result_topic_is_published_and_bound_to_its_handler() -> None:
    topic = "onex.evt.omnimarket.agent-review-pr-trigger-decided.v1"
    assert _PUBLISHED["PrReviewTriggerResult"] == topic
    assert topic in _CONTRACT["event_bus"]["publish_topics"]
    assert _return_type(HandlerPrReviewTrigger) is ModelPrReviewTriggerResult


def test_the_quorum_result_topic_is_published_and_bound_to_its_handler() -> None:
    topic = "onex.evt.omnimarket.agent-review-quorum-decided.v1"
    assert _PUBLISHED["AgentQuorumResult"] == topic
    assert topic in _CONTRACT["event_bus"]["publish_topics"]
    assert _return_type(HandlerAgentReviewQuorum) is ModelAgentQuorumResult

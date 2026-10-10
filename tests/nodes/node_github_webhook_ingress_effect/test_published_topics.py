# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each observation model publishes to the topic its contract declares."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_github_webhook_ingress_effect.models import (
    ModelGitHubBranchHeadObservation,
    ModelGitHubCheckRunObservation,
    ModelGitHubPrMergedObservation,
    ModelGitHubPrStateObservation,
    ModelGitHubWorkflowRunObservation,
)

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_github_webhook_ingress_effect"
    / "contract.yaml"
)


def _contract() -> dict[str, object]:
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


@pytest.mark.parametrize(
    ("topic", "model"),
    [
        ("onex.evt.github.pr-status.v1", ModelGitHubPrStateObservation),
        ("onex.evt.github.pr-merged.v1", ModelGitHubPrMergedObservation),
        ("onex.evt.github.branch-head.v1", ModelGitHubBranchHeadObservation),
        ("onex.evt.github.check-run.v1", ModelGitHubCheckRunObservation),
        ("onex.evt.github.workflow-run.v1", ModelGitHubWorkflowRunObservation),
    ],
)
def test_the_observation_model_defaults_to_a_declared_publish_topic(
    topic: str, model: type
) -> None:
    event_bus = _contract()["event_bus"]
    assert isinstance(event_bus, dict)
    assert topic in event_bus["publish_topics"]
    assert model.model_fields["topic"].default == topic


def test_the_failure_terminal_is_the_refused_delivery_topic() -> None:
    contract = _contract()
    dispatch = contract["runtime_dispatch"]
    assert isinstance(dispatch, dict)
    assert dispatch["terminal_events"] == [
        "onex.evt.github.webhook-delivery-refused.v1"
    ]
    event_bus = contract["event_bus"]
    assert isinstance(event_bus, dict)
    assert "onex.evt.github.webhook-delivery-refused.v1" in event_bus["publish_topics"]

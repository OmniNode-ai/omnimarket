# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pin the two producer shapes on onex.evt.github.pr-status.v1 against the branch-claim consumer.

This pins the two producer shapes; the poller shape has its own consumer route
(see test_seam_pr_status_route_coverage.py). The topic onex.evt.github.pr-status.v1 has two
producers: node_github_webhook_ingress_effect publishes
omnimarket.models.model_github_pr_state_observation.ModelGitHubPrStateObservation
and node_github_pr_poller_effect publishes
omnimarket.nodes.node_github_pr_poller_effect.models.model_github_pr_status_poll_event.ModelGitHubPrStatusPollEvent.

The branch-claim consumer (node_branch_claim_check_effect) declares, in its
contract.yaml handler_routing entry for that topic, the event_model
omnimarket.models.model_github_pr_state_observation.ModelGitHubPrStateObservation
with extra=forbid and required fields entity_id, delivery_id, github_event,
as_of, repo, pr_number. The runtime type-scopes on that model: a payload that
does not validate is dead-lettered to onex.dlq.omnimarket.branch-claim-check-malformed.v1.

These tests resolve the consumer's declared model FROM ITS CONTRACT (a yaml read
of contract.yaml, the handler_routing entry whose topic is
onex.evt.github.pr-status.v1, its event_model dotted string, imported with
importlib), never by importing the class directly. They then pin the two
producer shapes against that resolved model:

1. The webhook observation validates (positive control).
2. The poller event is refused (failing control, naming exactly what the poller
   shape lacks: entity_id, delivery_id, github_event, as_of).
3. The poller's default config names no repository (the reason the refusal in
   test 2 is dormant in the dev lane).
4. The consumer contract declares a dead-letter topic for refused payloads.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_github_pr_state_observation import (
    ModelGitHubPrStateObservation,
)
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_poller_config import (
    ModelGitHubPollerConfig,
)
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_pr_status_poll_event import (
    ModelGitHubPrStatusPollEvent,
)

pytestmark = pytest.mark.unit

_TOPIC = "onex.evt.github.pr-status.v1"
_DLQ_TOPIC = "onex.dlq.omnimarket.branch-claim-check-malformed.v1"


def _contract_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_branch_claim_check_effect"
        / "contract.yaml"
    )


def _consumer_event_model():
    """Resolve the consumer's declared event model from its contract.yaml.

    Reads the handler_routing entry whose topic is onex.evt.github.pr-status.v1,
    takes its event_model dotted string, and imports it with importlib. Never
    imports the class directly.
    """
    contract = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))
    handlers = contract["handler_routing"]["handlers"]
    entry = next(h for h in handlers if h["topic"] == _TOPIC)
    dotted = entry["event_model"]
    module_name, _, class_name = dotted.rpartition(".")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def test_webhook_observation_is_accepted_by_the_consumer_model():
    """A valid ModelGitHubPrStateObservation round-trips through the consumer's model."""
    producer_model = ModelGitHubPrStateObservation
    consumer_model = _consumer_event_model()

    observation = producer_model.model_validate(
        {
            "entity_id": "OmniNode-ai/omniclaude#1",
            "repo": "OmniNode-ai/omniclaude",
            "pr_number": 1,
            "delivery_id": str(uuid4()),
            "github_event": "pull_request",
            "as_of": datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC).isoformat(),
            "ci_status": "PENDING",
            "head_sha": "a" * 40,
            "head_ref": "lane/omn-1-change",
        }
    )

    wire = json.loads(observation.model_dump_json())
    validated = consumer_model.model_validate(wire)
    assert validated == observation


def test_poller_event_is_refused_by_the_consumer_model():
    """The poller's ModelGitHubPrStatusPollEvent is refused by the consumer's model.

    Failing control: the poller shape lacks entity_id, delivery_id, github_event
    and as_of, which the consumer's model requires. This is the shape that would
    be dead-lettered if the poller ever published to the consumer's topic.
    """
    consumer_model = _consumer_event_model()

    poller_event = ModelGitHubPrStatusPollEvent(
        repo="OmniNode-ai/omniclaude",
        pr_number=1,
        triage_state="ready_to_merge",
        title="A PR",
        is_draft=False,
        partition_key="OmniNode-ai/omniclaude:1",
        correlation_id=uuid4(),
    )

    wire = json.loads(poller_event.model_dump_json())
    with pytest.raises(ValidationError) as exc_info:
        consumer_model.model_validate(wire)

    locations = {e["loc"] for e in exc_info.value.errors()}
    for required in ("entity_id", "delivery_id", "github_event", "as_of"):
        assert (required,) in locations, f"{required} not flagged as missing"


def test_poller_default_config_names_no_repository():
    """The poller's default config names no repository, so the refusal is dormant."""
    config = ModelGitHubPollerConfig()
    assert config.repos == []


def test_consumer_contract_declares_a_dead_letter_topic_for_refused_payloads():
    """The consumer contract declares the dead-letter topic for refused payloads."""
    contract = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))
    dlq_topics = contract["event_bus"]["dlq_topics"]
    assert _DLQ_TOPIC in dlq_topics

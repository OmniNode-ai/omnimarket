# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every shape published on onex.evt.github.pr-status.v1 has a consumer route (OMN-24084).

The topic has two producers, the webhook ingress (ModelGitHubPrStateObservation)
and the poller (ModelGitHubPrStatusPollEvent). The runtime type-scopes each
handler_routing entry on its declared event_model, so a producer shape that no
entry of the branch-claim consumer accepts is dead-lettered
(onex.dlq.omnimarket.branch-claim-check-malformed.v1) on every delivery, which
held the dev runtime's health DEGRADED once before (see the docstring of
tests/nodes/node_github_pr_poller_effect/test_poller_publishes_only_status_events.py).

This test serializes each producer's event, reads the consumer's routes FROM ITS
CONTRACT and asserts that exactly one route accepts it; the poller shape's route
decides nothing and returns no result. On the merge base the poller shape has no
route, so the poller assertions fail there.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.models.model_github_pr_state_observation import (
    ModelGitHubPrStateObservation,
)
from omnimarket.nodes.node_branch_claim_check_effect.handlers.handler_branch_claim_check import (
    HandlerBranchClaimCheck,
)
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_pr_status_poll_event import (
    ModelGitHubPrStatusPollEvent,
)

pytestmark = pytest.mark.unit

_TOPIC = "onex.evt.github.pr-status.v1"
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_branch_claim_check_effect"
    / "contract.yaml"
)


def _route_models() -> list[type[BaseModel]]:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    models: list[type[BaseModel]] = []
    for entry in contract["handler_routing"]["handlers"]:
        if entry.get("topic") != _TOPIC:
            continue
        module_name, _, class_name = entry["event_model"].rpartition(".")
        models.append(getattr(importlib.import_module(module_name), class_name))
    return models


def _accepting(wire: dict[str, object]) -> list[type[BaseModel]]:
    accepted = []
    for model in _route_models():
        try:
            model.model_validate(wire)
        except ValidationError:
            continue
        accepted.append(model)
    return accepted


def _webhook_wire() -> dict[str, object]:
    event = ModelGitHubPrStateObservation(
        entity_id="OmniNode-ai/omnimarket#1",
        repo="OmniNode-ai/omnimarket",
        pr_number=1,
        delivery_id=uuid4(),
        github_event="pull_request",
        as_of=datetime(2026, 11, 13, 12, 0, tzinfo=UTC),
    )
    return json.loads(event.model_dump_json())


def _poller_wire() -> dict[str, object]:
    event = ModelGitHubPrStatusPollEvent(
        repo="OmniNode-ai/omnimarket",
        pr_number=1,
        triage_state="ready_to_merge",
        title="A PR",
        is_draft=False,
        partition_key="OmniNode-ai/omnimarket:1",
        correlation_id=uuid4(),
    )
    return json.loads(event.model_dump_json())


def test_webhook_observation_has_exactly_one_route() -> None:
    assert _accepting(_webhook_wire()) == [ModelGitHubPrStateObservation]


def test_poller_event_has_exactly_one_route() -> None:
    assert _accepting(_poller_wire()) == [ModelGitHubPrStatusPollEvent]


def test_poller_event_decides_nothing() -> None:
    poll = ModelGitHubPrStatusPollEvent.model_validate(_poller_wire())
    assert HandlerBranchClaimCheck().handle(poll) is None


def test_a_payload_no_producer_sends_has_no_route() -> None:
    assert _accepting({"nope": 1}) == []

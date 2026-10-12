# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every consumer of pr-state-observed accepts both schema versions."""

import json
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel, ValidationError

import omnimarket.nodes
from omnimarket.events.topics import PR_STATE_OBSERVED_TOPIC_V1
from omnimarket.nodes.node_pr_handoff_orchestrator.models.model_pr_handoff_observation_ingress import (
    ModelPrHandoffObservationIngress,
)
from omnimarket.nodes.node_projection_pr_state.handlers.pr_state_fold import (
    HandlerProjectionPrState,
)
from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_fold_request import (
    ModelPrStateFoldRequest,
)
from tests.unit.nodes.node_pr_state_emit_effect.helpers import event, event_v2

pytestmark = pytest.mark.unit

NODES_DIR = Path(omnimarket.nodes.__file__).parent


def enriched(e: Any) -> dict[str, Any]:
    return {**e.model_dump(mode="json"), "actor": "watcher", "lane": "dev"}


@pytest.mark.parametrize("make", [event, event_v2], ids=["v1", "v2"])
def test_projection_folds_either_version(make: Any) -> None:
    e = make()
    request = ModelPrStateFoldRequest.model_validate(enriched(e))
    assert request.event == e
    result = HandlerProjectionPrState().handle(request)
    assert result.event.digest == e.digest


def test_projection_refuses_a_version_2_payload_whose_digest_is_wrong() -> None:
    wire = enriched(event_v2())
    wire["digest"] = "0" * 64
    with pytest.raises(ValidationError, match="digest does not match"):
        ModelPrStateFoldRequest.model_validate(wire)


@pytest.mark.parametrize("make", [event, event_v2], ids=["v1", "v2"])
def test_handoff_ingress_takes_either_version(make: Any) -> None:
    e = make()
    ingress = ModelPrHandoffObservationIngress.model_validate(e.model_dump(mode="json"))
    assert ingress.repo == e.repo
    assert ingress.observation().red_contexts == e.red_contexts


# The keys the emit transport adds to every watcher observation on the way to
# the bus, with the values a dead letter on the dev broker carried.
TRANSPORT_ENRICHMENT: dict[str, Any] = {
    "actor": "claude",
    "lane": "",
    "lane_source": "unresolved",
    "lane_ticket": "",
    "turn_id": None,
    "workspace_path": "",
    "hook_fired_at": "2026-10-12T10:36:12.123507+00:00",
    "correlation_id": "0" * 64,
    "causation_id": None,
    "emitted_at": "2026-10-12T10:36:20.405050+00:00",
    "schema_version": "1.0.0",
}


def transported(e: Any) -> dict[str, Any]:
    """The wire payload as consumers read it; a version 2 event keeps its own schema_version."""
    return {**TRANSPORT_ENRICHMENT, **e.model_dump(mode="json")}


def routed_event_models() -> list[type[BaseModel]]:
    """Every event model a node contract routes pr-state-observed to."""
    models: list[type[BaseModel]] = []
    for path in sorted(NODES_DIR.glob("node_*/contract.yaml")):
        contract = yaml.safe_load(path.read_text())
        routing = (contract.get("handler_routing") or {}).get("handlers") or []
        for entry in routing:
            if entry.get("topic") != PR_STATE_OBSERVED_TOPIC_V1:
                continue
            ref = entry["event_model"]
            models.append(getattr(import_module(ref["module"]), ref["name"]))
    return models


@pytest.mark.parametrize("make", [event, event_v2], ids=["v1", "v2"])
def test_handoff_ingress_takes_the_transported_payload(make: Any) -> None:
    e = make()
    ingress = ModelPrHandoffObservationIngress.model_validate(transported(e))
    assert ingress.digest == e.digest
    assert ingress.handoff_key == f"{e.repo}#{e.pr_number}"
    assert ingress.observation().red_contexts == e.red_contexts


def test_handoff_ingress_still_refuses_a_mismatched_handoff_key() -> None:
    wire = {**transported(event()), "handoff_key": "other/repo#1"}
    with pytest.raises(ValidationError, match="does not match"):
        ModelPrHandoffObservationIngress.model_validate(wire)


def test_every_routed_subscriber_takes_the_transported_payload() -> None:
    models = routed_event_models()
    assert ModelPrHandoffObservationIngress in models  # positive control on the scan
    assert len(models) >= 3
    for make in (event, event_v2):
        wire = transported(make())
        for model in models:
            model.model_validate(wire)
            model.model_validate_json(json.dumps(wire))

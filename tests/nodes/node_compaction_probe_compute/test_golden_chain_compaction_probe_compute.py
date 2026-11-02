# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_compaction_probe_compute: contract -> handler -> typed result.

The node is pure and declares no topic: the chain resolves each handler the
contract routes to, runs the clean fixture through it, and checks the typed
result. The error chain feeds a request with a blank correlation id and expects
a validation refusal with no result.
"""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import omnimarket.nodes.node_compaction_probe_compute as node_package
from omnimarket.nodes.node_compaction_probe_compute.models.model_compaction_probe import (
    EnumProbeAudit,
    ModelCompactionProbeRequest,
    ModelCompactionProbeResult,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
FIXTURE = Path(__file__).parent / "fixtures" / "effective_rulings.json"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_contract_declares_no_topic_and_a_pure_descriptor() -> None:
    contract = _contract()
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert "terminal_event" not in contract
    descriptor = contract["descriptor"]
    assert isinstance(descriptor, dict)
    assert descriptor["purity"] == "pure"


def test_entry_point_is_registered() -> None:
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_compaction_probe_compute"].load() is node_package


def test_golden_chain_contract_handler_builds_probes_and_contexts() -> None:
    routing = _contract()["handler_routing"]
    assert isinstance(routing, dict)
    by_operation = {route["operation"]: route for route in routing["handlers"]}
    route = by_operation["build_compaction_probes"]
    module = importlib.import_module(route["handler"]["module"])
    handler = getattr(module, route["handler"]["name"])()
    request = ModelCompactionProbeRequest.model_validate(
        json.loads(FIXTURE.read_text())["clean"]
    )
    result = handler.handle(request)
    assert isinstance(result, ModelCompactionProbeResult)
    assert result.correlation_id == "fixture-cm7-clean"
    assert [p.audit for p in result.probes] == [EnumProbeAudit.ACCEPTED] * 2
    assert result.contexts.oracle.text
    assert result.contexts.empty.text == ""
    assert result.contexts.inverted.text
    assert "judge_probe_answer" in by_operation


def test_error_chain_blank_correlation_id_is_refused() -> None:
    payload = json.loads(FIXTURE.read_text())["clean"]
    with pytest.raises(ValidationError):
        ModelCompactionProbeRequest.model_validate({**payload, "correlation_id": ""})

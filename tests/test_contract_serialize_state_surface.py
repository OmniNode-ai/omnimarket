# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State-surface coverage for the model->contract serialization nodes.

Pins the declared output keys and published event topics of every serialization
node (parent + four leaves) as literals, and asserts each node's ``contract.yaml``
declares exactly that surface. This both wires the contract-state-coverage gate
and guards against silent drift between a node's output model and its contract.

Node names referenced here (so the coverage gate associates this file with them):
  node_subcontract_render_compute
  node_advanced_features_resolve_compute
  node_contract_assemble_compute
  node_contract_digest_compute
  node_contract_serialize_compute
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_NODES_DIR = Path("src/omnimarket/nodes")

# Declared state surface per node: output-block keys + published event topics.
# The literals here are what the contract-state-coverage gate looks for.
_EXPECTED: dict[str, dict[str, set[str]]] = {
    "node_subcontract_render_compute": {
        "outputs": {"type", "yaml_fragment", "sha256"},
        "publish_topics": {
            "onex.evt.omnimarket.subcontract-render-completed.v1",
        },
    },
    "node_advanced_features_resolve_compute": {
        "outputs": {
            "circuit_breaker",
            "retry",
            "observability",
            "dead_letter_queue_enabled",
            "transactions_enabled",
        },
        "publish_topics": {
            "onex.evt.omnimarket.advanced-features-resolve-completed.v1",
        },
    },
    "node_contract_assemble_compute": {
        "outputs": {"contract_yaml"},
        "publish_topics": {
            "onex.evt.omnimarket.contract-assemble-completed.v1",
        },
    },
    "node_contract_digest_compute": {
        "outputs": {"contract_sha256"},
        "publish_topics": {
            "onex.evt.omnimarket.contract-digest-completed.v1",
        },
    },
    "node_contract_serialize_compute": {
        "outputs": {
            "contract_yaml",
            "contract_sha256",
            "subcontracts_rendered",
            "lint_status",
            "lint_messages",
        },
        "publish_topics": {
            "onex.evt.omnimarket.contract-serialize-completed.v1",
        },
    },
}


def _contract(node_name: str) -> dict[str, object]:
    return yaml.safe_load((_NODES_DIR / node_name / "contract.yaml").read_text())


@pytest.mark.unit
@pytest.mark.parametrize("node_name", sorted(_EXPECTED))
def test_contract_declares_expected_outputs(node_name: str) -> None:
    contract = _contract(node_name)
    outputs = contract.get("outputs") or {}
    assert set(outputs.keys()) == _EXPECTED[node_name]["outputs"]


@pytest.mark.unit
@pytest.mark.parametrize("node_name", sorted(_EXPECTED))
def test_contract_declares_expected_publish_topics(node_name: str) -> None:
    contract = _contract(node_name)
    event_bus = contract.get("event_bus") or {}
    publish = set(event_bus.get("publish_topics") or [])
    assert publish == _EXPECTED[node_name]["publish_topics"]


@pytest.mark.unit
@pytest.mark.parametrize("node_name", sorted(_EXPECTED))
def test_terminal_event_matches_published_completion_topic(node_name: str) -> None:
    contract = _contract(node_name)
    assert contract["terminal_event"] in _EXPECTED[node_name]["publish_topics"]

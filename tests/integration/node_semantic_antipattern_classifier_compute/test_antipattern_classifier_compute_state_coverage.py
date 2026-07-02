# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared surface pin for node_semantic_antipattern_classifier_compute.

OMN-13674 (cluster wave-semantic-antipattern-subsystem, archetype compute).

A COMPUTE node has no fsm; its declared "state space" is its declared outputs
(``violations``, ``has_blocking_violation``) and its single classify route + topics.
This pins those declared surfaces against the literals the bus-coverage suite drives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from tests.integration.node_semantic_antipattern_classifier_compute.test_antipattern_classifier_compute_bus_coverage import (
    TOPIC_CLASSIFIED,
    TOPIC_CLASSIFY,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_semantic_antipattern_classifier_compute"
    / "contract.yaml"
)


def _load_contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text()))


def test_contract_is_pure_compute() -> None:
    contract = _load_contract()
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["node_archetype"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"


def test_contract_declares_outputs() -> None:
    outputs = _load_contract()["outputs"]
    assert "violations" in outputs
    assert "has_blocking_violation" in outputs


def test_contract_declares_single_classify_route() -> None:
    handlers = _load_contract()["handler_routing"]["handlers"]
    operations = {h["operation"] for h in handlers}
    assert operations == {"classify_antipattern_matches"}


def test_contract_topics_are_literal() -> None:
    event_bus = _load_contract()["event_bus"]
    assert TOPIC_CLASSIFY in event_bus["subscribe_topics"]
    assert TOPIC_CLASSIFIED in event_bus["publish_topics"]


def test_contract_terminal_event_is_classified_topic() -> None:
    assert _load_contract()["terminal_event"] == TOPIC_CLASSIFIED

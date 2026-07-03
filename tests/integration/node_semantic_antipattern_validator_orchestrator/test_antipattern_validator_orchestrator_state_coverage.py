# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared surface pin for node_semantic_antipattern_validator_orchestrator.

OMN-13674 (cluster wave-semantic-antipattern-subsystem, archetype orchestrator).

This ORCHESTRATOR declares no ``fsm`` block -- its declared state space is the
single ``handler_routing`` route plus the subscribe/publish/terminal topics. This
module pins those declared wire strings + route against the literals the
bus-coverage suite drives, so a silent contract edit (renamed topic, dropped route)
fails here rather than only at a live runtime boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from tests.integration.node_semantic_antipattern_validator_orchestrator.test_antipattern_validator_orchestrator_bus_coverage import (
    TOPIC_MATCH_REQUESTED,
    TOPIC_VALIDATE,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_semantic_antipattern_validator_orchestrator"
    / "contract.yaml"
)

_TERMINAL_EVENT = "onex.evt.omnimarket.antipattern-validated.v1"


def _load_contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text()))


def test_contract_is_orchestrator() -> None:
    contract = _load_contract()
    assert contract["node_type"] == "orchestrator"
    assert contract["descriptor"]["node_archetype"] == "orchestrator"


def test_contract_declares_single_validate_route() -> None:
    """The one declared route the bus-coverage suite drives."""
    handlers = _load_contract()["handler_routing"]["handlers"]
    operations = {h["operation"] for h in handlers}
    assert operations == {"validate_semantic_antipatterns"}


def test_contract_subscribe_and_publish_topics_are_literal() -> None:
    event_bus = _load_contract()["event_bus"]
    assert TOPIC_VALIDATE in event_bus["subscribe_topics"]
    assert TOPIC_MATCH_REQUESTED in event_bus["publish_topics"]


def test_contract_terminal_event_literal() -> None:
    assert _load_contract()["terminal_event"] == _TERMINAL_EVENT


def test_contract_declares_similarity_threshold_config_default() -> None:
    """The config default forwarded into the emitted command is pinned at 0.80."""
    config = _load_contract()["config"]
    assert config["similarity_threshold"]["default"] == 0.80

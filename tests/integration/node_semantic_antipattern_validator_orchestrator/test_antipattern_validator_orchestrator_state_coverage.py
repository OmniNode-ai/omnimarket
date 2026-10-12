# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared surface pin for node_semantic_antipattern_validator_orchestrator.

OMN-13674 (cluster wave-semantic-antipattern-subsystem, archetype orchestrator).

This ORCHESTRATOR declares no ``fsm`` block -- its declared state space is the
three ``handler_routing`` routes (the start command and the two answers it waits
on) plus the subscribe/publish/terminal topics. This module pins those declared
wire strings + routes against the literals the bus-coverage suite drives, and the
topics it shares with the match effect and the classifier against those nodes' own
contracts, so a silent contract edit (renamed topic, dropped route, a topic one
side no longer publishes) fails here rather than only at a live runtime boundary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from tests.integration.node_semantic_antipattern_validator_orchestrator.test_golden_chain_antipattern_validator_orchestrator import (
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
_TOPIC_CLASSIFY = "onex.cmd.omnimarket.antipattern-classify.v1"
_TOPIC_MATCH_RESPONSE = "onex.evt.omnimarket.antipattern-match-response.v1"
_TOPIC_CLASSIFIED = "onex.evt.omnimarket.antipattern-classified.v1"


def _load_contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text()))


def test_contract_is_orchestrator() -> None:
    contract = _load_contract()
    assert contract["node_type"] == "orchestrator"
    assert contract["descriptor"]["node_archetype"] == "orchestrator"


def _node_contract(node: str) -> dict[str, Any]:
    path = _CONTRACT_PATH.parents[1] / node / "contract.yaml"
    return cast(dict[str, Any], yaml.safe_load(path.read_text()))


def test_contract_declares_the_three_hop_routes() -> None:
    """The start command and the two answers the orchestrator waits on."""
    handlers = _load_contract()["handler_routing"]["handlers"]
    operations = {h["operation"] for h in handlers}
    assert operations == {
        "validate_semantic_antipatterns",
        "dispatch_antipattern_matches",
        "emit_antipattern_validated",
    }


def test_every_route_names_its_topic_category_and_model() -> None:
    """A mixed-category contract needs an explicit message_category per entry, else
    a topic matches zero routes and is consumed and committed with nothing handled."""
    contract = _load_contract()
    subscribed = contract["event_bus"]["subscribe_topics"]
    handlers = contract["handler_routing"]["handlers"]
    assert [h["topic"] for h in handlers] == subscribed
    for handler in handlers:
        topic = handler["topic"]
        expected = "command" if ".cmd." in topic else "event"
        assert handler["message_category"] == expected, topic
        assert handler["event_model"]["name"], topic
    assert len({h["operation"] for h in handlers}) == len(handlers)
    assert len({h["event_model"]["name"] for h in handlers}) == len(handlers)


def test_published_events_cover_every_publish_topic_once() -> None:
    """Each returned model class maps to one declared publish topic."""
    contract = _load_contract()
    mapped = [e["topic"] for e in contract["published_events"]]
    assert sorted(mapped) == sorted(contract["event_bus"]["publish_topics"])
    assert len(set(mapped)) == len(mapped)


def test_terminal_event_is_a_publish_topic() -> None:
    contract = _load_contract()
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]


def test_orchestrator_topics_meet_the_match_effect_and_classifier_topics() -> None:
    """The three nodes only chain if one publishes what the next subscribes to."""
    orchestrator = _load_contract()["event_bus"]
    match = _node_contract("node_antipattern_match_effect")["event_bus"]
    classifier = _node_contract("node_semantic_antipattern_classifier_compute")[
        "event_bus"
    ]
    assert match["subscribe_topics"][0] in orchestrator["publish_topics"]
    assert match["publish_topics"][0] in orchestrator["subscribe_topics"]
    assert classifier["subscribe_topics"][0] in orchestrator["publish_topics"]
    assert classifier["publish_topics"][0] in orchestrator["subscribe_topics"]


def test_contract_subscribe_and_publish_topics_are_literal() -> None:
    event_bus = _load_contract()["event_bus"]
    assert TOPIC_VALIDATE in event_bus["subscribe_topics"]
    assert TOPIC_MATCH_REQUESTED in event_bus["publish_topics"]


def test_contract_hop_topics_are_literal() -> None:
    """The classify command it publishes and the two answers it subscribes to."""
    event_bus = _load_contract()["event_bus"]
    assert _TOPIC_CLASSIFY in event_bus["publish_topics"]
    assert _TOPIC_MATCH_RESPONSE in event_bus["subscribe_topics"]
    assert _TOPIC_CLASSIFIED in event_bus["subscribe_topics"]


def test_contract_terminal_event_literal() -> None:
    assert _load_contract()["terminal_event"] == _TERMINAL_EVENT


def test_contract_declares_similarity_threshold_config_default() -> None:
    """The config default forwarded into the emitted command is pinned at 0.80."""
    config = _load_contract()["config"]
    assert config["similarity_threshold"]["default"] == 0.80

# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_intelligence_reducer"
    / "contract.yaml"
)


def test_intelligence_reducer_contract_declares_runtime_topics() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    event_bus = data["event_bus"]

    assert event_bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.pattern-lifecycle-process.v1",
        "onex.evt.omnimarket.intent-outcome-labeled.v1",
    ]
    assert event_bus["publish_topics"] == [
        "onex.evt.omnimarket.intent-pattern-promoted.v1"
    ]


def test_intelligence_reducer_contract_uses_omnimarket_modules() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())

    assert data["input_model"]["module"] == (
        "omnimarket.nodes.node_intelligence_reducer.models"
    )
    assert data["output_model"]["module"] == (
        "omnimarket.nodes.node_intelligence_reducer.models"
    )
    assert data["state_model"]["module"] == (
        "omnimarket.nodes.node_intelligence_reducer.models"
    )

    publish_meta = data["event_bus"]["publish_topic_metadata"][
        "onex.evt.omnimarket.intent-pattern-promoted.v1"
    ]
    subscribe_meta = data["event_bus"]["subscribe_topic_metadata"][
        "onex.evt.omnimarket.intent-outcome-labeled.v1"
    ]
    assert publish_meta["schema_ref"] == (
        "omnimarket.intelligence.events.ModelIntentPatternPromotedEnvelope"
    )
    assert subscribe_meta["schema_ref"] == (
        "omnimarket.intelligence.events.ModelIntentOutcomeLabeledEnvelope"
    )

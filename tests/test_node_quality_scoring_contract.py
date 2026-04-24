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
    / "node_quality_scoring_compute"
    / "contract.yaml"
)


def test_quality_scoring_contract_declares_runtime_topics() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    event_bus = data["event_bus"]

    assert event_bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.quality-assessment.v1"
    ]
    assert event_bus["publish_topics"] == [
        "onex.evt.omnimarket.quality-assessment-completed.v1"
    ]


def test_quality_scoring_contract_uses_omnimarket_models() -> None:
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    event_bus = data["event_bus"]

    subscribe_meta = event_bus["subscribe_topic_metadata"][
        "onex.cmd.omnimarket.quality-assessment.v1"
    ]
    publish_meta = event_bus["publish_topic_metadata"][
        "onex.evt.omnimarket.quality-assessment-completed.v1"
    ]

    assert subscribe_meta["schema_ref"].startswith(
        "omnimarket.nodes.node_quality_scoring_compute.models."
    )
    assert publish_meta["schema_ref"].startswith(
        "omnimarket.nodes.node_quality_scoring_compute.models."
    )

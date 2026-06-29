# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Contract-shape tests for node_user_correction_observer_effect (OMN-12846)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_user_correction_observer_effect"
    / "contract.yaml"
)


@pytest.mark.unit
def test_observer_contract_is_effect_node() -> None:
    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    assert data["node_type"] == "effect"
    assert data["name"] == "node_user_correction_observer_effect"


@pytest.mark.unit
def test_observer_contract_declares_publish_topic() -> None:
    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    event_bus = data["event_bus"]
    assert event_bus["publish_topics"] == ["onex.evt.omnimarket.user-correction.v1"]


@pytest.mark.unit
def test_observer_contract_publish_schema_ref_is_correction_event() -> None:
    data = yaml.safe_load(_CONTRACT_PATH.read_text())
    publish_meta = data["event_bus"]["publish_topic_metadata"][
        "onex.evt.omnimarket.user-correction.v1"
    ]
    assert (
        publish_meta["schema_ref"]
        == "omnimarket.intelligence.events.ModelUserCorrectionEvent"
    )

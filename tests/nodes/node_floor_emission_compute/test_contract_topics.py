# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topics node_floor_emission_compute declares are the topics the 17a floor documents (OMN-19998)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_emission_compute/contract.yaml"
)


def _bus() -> dict[str, list[str]]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    bus: dict[str, list[str]] = loaded["event_bus"]
    return bus


def test_emission_publishes_its_declared_topics() -> None:
    published = _bus()["publish_topics"]
    assert "onex.evt.omnimarket.floor-node-started.v1" in published
    assert "onex.evt.omnimarket.floor-node-succeeded.v1" in published
    assert "onex.evt.omnimarket.floor-node-failed.v1" in published
    assert "onex.evt.omnimarket.floor-receipts-computed.v1" in published


def test_emission_terminal_event_is_published() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["terminal_event"] == "onex.evt.omnimarket.floor-receipts-computed.v1"
    assert loaded["terminal_event"] in _bus()["publish_topics"]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topics node_lab_state_alarm_effect declares are the topics the 17a floor documents (OMN-19998)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_state_alarm_effect/contract.yaml"
)


def _bus() -> dict[str, list[str]]:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    bus: dict[str, list[str]] = loaded["event_bus"]
    return bus


def test_alarm_publishes_its_declared_topics() -> None:
    published = _bus()["publish_topics"]
    assert "onex.evt.omnimarket.lab-state-alarmed.v1" in published


def test_alarm_terminal_event_is_published() -> None:
    loaded = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert loaded["terminal_event"] == "onex.evt.omnimarket.lab-state-alarmed.v1"
    assert loaded["terminal_event"] in _bus()["publish_topics"]

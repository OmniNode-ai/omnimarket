# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract of node_host_drain_effect declares its terminal event and the topics by role (OMN-22905)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_host_drain_effect as node_pkg


@pytest.fixture
def contract() -> dict[str, Any]:
    path = Path(node_pkg.__file__).parent / "contract.yaml"
    loaded: dict[str, Any] = yaml.safe_load(path.read_text())
    return loaded


@pytest.mark.unit
def test_terminal_event_is_the_completed_topic(contract: dict[str, Any]) -> None:
    assert contract["terminal_event"] == "onex.evt.omnimarket.host-drain-completed.v1"
    assert contract["terminal_event"] == contract["topics"]["host_drain_completed"]


@pytest.mark.unit
def test_completed_topic_is_externally_consumed(contract: dict[str, Any]) -> None:
    assert contract["terminal_event"] in contract["externally_consumed_topics"]

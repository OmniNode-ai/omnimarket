# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract of node_host_down_reading_effect declares its topics by role and its terminal event (OMN-19998)."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_host_down_reading_effect as node_pkg
from omnimarket.nodes.node_host_down_reading_effect.runtime_host_down import (
    reading_topic,
)

pytestmark = [pytest.mark.unit]


@pytest.fixture
def contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (Path(node_pkg.__file__).parent / "contract.yaml").read_text()
    )
    return loaded


def test_terminal_event_is_the_sweep_completed_topic(contract: dict[str, Any]) -> None:
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.host-down-sweep-completed.v1"
    )
    assert contract["terminal_event"] == contract["topics"]["host_down_sweep_completed"]


def test_both_topics_are_externally_consumed(contract: dict[str, Any]) -> None:
    assert set(contract["topics"].values()) <= set(
        contract["externally_consumed_topics"]
    )
    assert {e["topic"] for e in contract["published_events"]} == set(
        contract["topics"].values()
    )


def test_the_reading_topic_the_handler_publishes_on_is_the_contracts(
    contract: dict[str, Any],
) -> None:
    assert reading_topic() == contract["topics"]["host_down_reading"]


def test_the_handler_block_names_a_class_that_imports_and_handles(
    contract: dict[str, Any],
) -> None:
    module = import_module(contract["handler"]["module"])
    assert hasattr(getattr(module, contract["handler"]["class"]), "handle")
    assert contract["handler_routing"]["handlers"] == []

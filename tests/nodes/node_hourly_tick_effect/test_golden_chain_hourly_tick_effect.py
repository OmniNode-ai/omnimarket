# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the packaged hourly tick contract: its operations, topics and handler."""

from __future__ import annotations

import importlib
from importlib.resources import files

import pytest
import yaml

pytestmark = pytest.mark.unit

NAME = "node_hourly_tick_effect"
COMMAND = "onex.cmd.omnimarket.hourly-tick.v1"
TERMINAL = "onex.evt.omnimarket.hourly-tick-completed.v1"


def test_golden_chain_hourly_tick_effect() -> None:
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    assert contract["name"] == NAME
    assert contract["node_type"] == "effect"
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL]
    assert contract["terminal_event"] == TERMINAL
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND

    routing = {
        entry["operation"]: entry for entry in contract["handler_routing"]["handlers"]
    }
    assert set(routing) == {"prepare", "record"}
    for entry in routing.values():
        module = importlib.import_module(entry["handler"]["module"])
        assert hasattr(module, entry["handler"]["name"])

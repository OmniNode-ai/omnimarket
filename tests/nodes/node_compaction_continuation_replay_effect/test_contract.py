# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract chain for node_compaction_continuation_replay_effect: topics and handler routing."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_compaction_continuation_replay_effect as node_package

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMPLETED = "onex.evt.omnimarket.compaction-continuation-replay-completed.v1"
START = "onex.cmd.omnimarket.compaction-continuation-replay-start.v1"


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_topics_are_declared_in_the_contract() -> None:
    contract = _contract()
    assert contract["terminal_event"] == COMPLETED
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert bus["publish_topics"] == [COMPLETED]
    assert bus["subscribe_topics"] == [START]


def test_each_routed_handler_resolves() -> None:
    routing = _contract()["handler_routing"]
    assert isinstance(routing, dict)
    operations = []
    for entry in routing["handlers"]:
        module = importlib.import_module(entry["handler"]["module"])
        assert callable(getattr(module, entry["handler"]["name"]).handle)
        operations.append(entry["operation"])
    assert operations == ["replay_continuation", "grade_repeat_stability"]

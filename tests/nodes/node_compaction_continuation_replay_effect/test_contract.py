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


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_topics_are_declared_in_the_contract() -> None:
    contract = _contract()
    completed = contract["terminal_event"]
    start = contract["runtime_dispatch"]["command_topic"]
    assert completed != start
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert bus["publish_topics"] == [completed]
    assert bus["subscribe_topics"] == [start]
    from omnimarket.testing.publisher_contract_fixture import publisher_event_type

    assert publisher_event_type(completed) == publisher_event_type(
        "onex.evt.omnimarket.compaction-continuation-replay-completed.v1"
    )


def test_each_routed_handler_resolves() -> None:
    routing = _contract()["handler_routing"]
    assert isinstance(routing, dict)
    operations = []
    for entry in routing["handlers"]:
        module = importlib.import_module(entry["handler"]["module"])
        assert callable(getattr(module, entry["handler"]["name"]).handle)
        operations.append(entry["operation"])
    assert operations == ["run_continuation_experiment"]


def test_deploy_contract_has_declarative_node_and_injected_dependencies() -> None:
    from omnibase_core.nodes.node_effect import NodeEffect

    assert issubclass(node_package.NodeCompactionContinuationReplayEffect, NodeEffect)
    contract = _contract()
    assert contract["handler"]["class"] == "HandlerReplayExperiment"
    assert contract["continuation"]["repeats"] == 3
    assert contract["continuation"]["default_turns"] == 25
    assert {d["name"] for d in contract["dependencies"]} >= {
        "ProtocolContinuationModel",
        "ProtocolActionScorer",
        "scratch_root",
    }
    assert (NODE_DIR / contract["continuation"]["stability_data_file"]).is_file()

    from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.handler_continuation_replay import (
        HandlerContinuationReplay,
    )
    from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.handler_repeat_stability import (
        HandlerRepeatStability,
    )
    from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.policy import (
        load_component_handler,
    )

    assert (
        load_component_handler("replay", HandlerContinuationReplay)
        is HandlerContinuationReplay
    )
    assert (
        load_component_handler("stability", HandlerRepeatStability)
        is HandlerRepeatStability
    )

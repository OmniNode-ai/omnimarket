# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract wiring of node_compaction_probe_run_effect: topics, routing, handler import."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_compaction_probe_run_effect as node_package

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.compaction-probe-run-completed.v1"
COMMAND = "onex.cmd.omnimarket.compaction-probe-run-start.v1"


def _contract() -> dict[str, object]:
    path = Path(node_package.__file__).parent / "contract.yaml"
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_terminal_event_is_the_published_topic() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert contract["terminal_event"] == TERMINAL
    assert bus["publish_topics"] == [TERMINAL]
    assert bus["subscribe_topics"] == [COMMAND]


def test_every_routed_handler_resolves_and_handles() -> None:
    routing = _contract()["handler_routing"]
    assert isinstance(routing, dict)
    operations = {entry["operation"] for entry in routing["handlers"]}
    assert operations == {"run_compaction_probes", "calibrate_probe_judge"}
    for entry in routing["handlers"]:
        module = importlib.import_module(entry["handler"]["module"])
        handler = getattr(module, entry["handler"]["name"])
        assert callable(handler(None).handle)
        for model in (entry["input_model"], entry["output_model"]):
            module_name, _, class_name = model.rpartition(".")
            assert hasattr(importlib.import_module(module_name), class_name)

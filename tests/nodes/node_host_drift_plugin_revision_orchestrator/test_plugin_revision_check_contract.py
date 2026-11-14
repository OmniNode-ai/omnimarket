# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract binding checks for the plugin revision check orchestrator node."""

import importlib
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml

pytestmark = pytest.mark.unit
PACKAGE = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_host_drift_plugin_revision_orchestrator"
)


def test_revision_check_contract_bindings() -> None:
    contract = yaml.safe_load((PACKAGE / "contract.yaml").read_text())
    assert contract["name"] == "node_host_drift_plugin_revision_orchestrator"
    assert contract["node_type"] == "ORCHESTRATOR_GENERIC"
    assert contract["descriptor"]["node_archetype"] == "orchestrator"
    assert contract["descriptor"]["purity"] == "effectful"
    command_topic = "onex.cmd.omnimarket.host-drift-plugin-revision-check-requested.v1"
    assert command_topic in contract["event_bus"]["subscribe_topics"]
    assert command_topic == contract["runtime_dispatch"]["command_topic"]
    terminal_topic = "onex.evt.omnimarket.host-drift-plugin-revision-checked.v1"
    assert terminal_topic == contract["terminal_event"]
    assert terminal_topic in contract["event_bus"]["publish_topics"]
    assert terminal_topic in [p["topic"] for p in contract["published_events"]]
    binding = contract["handler"]
    handler = getattr(importlib.import_module(binding["module"]), binding["class"])
    hints = get_type_hints(handler.handle)
    for key, hint in (("input_model", "request"), ("output_model", "return")):
        module, _, name = binding[key].rpartition(".")
        model = getattr(importlib.import_module(module), name)
        assert hints[hint] is model
        declaration = contract[key]
        assert (
            getattr(importlib.import_module(declaration["module"]), declaration["name"])
            is model
        )

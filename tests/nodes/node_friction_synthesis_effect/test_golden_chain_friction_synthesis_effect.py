# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve and execute the packaged friction synthesis contract through its bus route."""

from __future__ import annotations

import importlib
import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from .test_friction_synthesis import harvest

pytestmark = pytest.mark.unit


def test_golden_chain_friction_synthesis_effect(tmp_path: Path) -> None:
    name = "node_friction_synthesis_effect"
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{name}").joinpath("contract.yaml").read_text()
    )
    assert contract["name"] == name
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert contract["terminal_event"] == "onex.evt.omnimarket.friction-synthesized.v1"
    (entry,) = contract["handler_routing"]["handlers"]
    assert entry["operation"] == "synthesize"
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    input_module, _, input_name = entry["input_model"].rpartition(".")
    output_module, _, output_name = entry["output_model"].rpartition(".")
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)
    source = tmp_path / "source.json"
    output = tmp_path / "merged.json"
    source.write_text(json.dumps(harvest([1, 2])))
    result = handler_type().handle(request_type(handoffs=[source], output=output))
    assert isinstance(result, result_type)
    assert result.exit_code == 0
    assert json.loads(output.read_text())["candidates"][0]["occurrences_this_run"] == 2

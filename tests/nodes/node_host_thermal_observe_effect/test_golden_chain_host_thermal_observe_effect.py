# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve and execute the packaged host thermal observe contract over a fake host."""

from __future__ import annotations

import importlib
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from .test_host_thermal_observe import _handler, _host

pytestmark = pytest.mark.unit


def test_golden_chain_host_thermal_observe_effect(tmp_path: Path) -> None:
    name = "node_host_thermal_observe_effect"
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{name}").joinpath("contract.yaml").read_text()
    )
    assert contract["name"] == name
    assert contract["node_type"] == "effect"
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert contract["terminal_event"] == "onex.evt.omnimarket.host-thermal-observed.v1"
    routing = contract["handler_routing"]
    assert routing["routing_strategy"] == "operation_match"
    assert [entry["operation"] for entry in routing["handlers"]] == ["observe"]
    entry = routing["handlers"][0]
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    input_module, _, input_name = entry["input_model"].rpartition(".")
    output_module, _, output_name = entry["output_model"].rpartition(".")
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)
    hwmon, proc = _host(tmp_path)
    handler = _handler(hwmon, proc)
    assert isinstance(handler, handler_type)
    state = tmp_path / "state"
    result = handler.handle(request_type(host="lab-a", state_dir=state))
    assert isinstance(result, result_type)
    assert result.host == "lab-a"
    assert result.cpu_temp_c == 91.5
    assert (state / "readings.jsonl").read_text().count("\n") == 1
    assert (state / "latest.json").is_file()

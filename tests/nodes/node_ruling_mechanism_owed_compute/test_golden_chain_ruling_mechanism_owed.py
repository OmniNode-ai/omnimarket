# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered package -> contract -> typed handler -> typed result, on the slice of 2026-10-24."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODE = "node_ruling_mechanism_owed_compute"
FIXTURE = Path(__file__).parent / "fixtures" / "ledger_slice_2026_10_24.txt"


def test_registered_contract_executes_the_recorded_slice() -> None:
    registered = [e for e in entry_points(group="onex.nodes") if e.name == NODE]
    assert len(registered) == 1
    package = registered[0].load()
    contract = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text(encoding="utf-8")
    )
    assert contract["name"] == NODE
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"

    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.ruling-mechanism-owed-requested.v1"
    ]
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.ruling-mechanism-owed-computed.v1"
    ]
    assert contract["terminal_event"] in bus["publish_topics"]
    assert contract["runtime_dispatch"]["command_topic"] == bus["subscribe_topics"][0]

    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    request_module, request_name = binding["input_model"].rsplit(".", 1)
    request_type = getattr(importlib.import_module(request_module), request_name)
    rows = [
        line
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    result = handler_type().handle(
        request_type.model_validate({"rows": rows, "tick_at": "2026-10-24T13:30:00Z"})
    )
    assert [row.key for row in result.owed] == [
        "mechanism-owed:OMN-18594:2026-10-24T12:06:44Z",
        "mechanism-owed:OMN-21288:2026-10-24T12:52:12Z",
    ]

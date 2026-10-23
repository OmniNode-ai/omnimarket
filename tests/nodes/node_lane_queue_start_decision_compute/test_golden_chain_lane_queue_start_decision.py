# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered package -> contract -> typed handler -> typed result, on the queue recorded on 2026-10-23."""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODE = "node_lane_queue_start_decision_compute"
FIXTURE = Path(__file__).parent / "fixtures" / "lane_queue_2026_10_23.json"


def test_registered_contract_executes_the_recorded_queue() -> None:
    entries = [e for e in entry_points(group="onex.nodes") if e.name == NODE]
    assert len(entries) == 1
    package = entries[0].load()
    contract = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text(encoding="utf-8")
    )
    assert contract["name"] == NODE
    assert contract["node_type"] == "compute"

    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    input_module, input_name = binding["input_model"].rsplit(".", 1)
    request_type = getattr(importlib.import_module(input_module), input_name)

    facts = json.loads(FIXTURE.read_text(encoding="utf-8"))
    request = request_type.model_validate(
        {
            "now": facts["now"],
            "priority_classes": [
                "mvp-landing",
                "occ-retirement",
                "process-fix",
                "other",
            ],
            "entries": facts["entries"],
            "hosts": facts["hosts"],
        }
    )
    result = handler_type().handle(request)
    assert next(s.lane for s in result.starts) == "lane-queue-start-1400"
    assert len(result.starts) == 8
    assert [h.reason_code for h in result.held] == ["no-free-slot"]

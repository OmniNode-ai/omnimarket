# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing stall fold's contract names a pure node and its handler (OMN-17427)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_landing_stall_expectation_compute"
)


def test_landing_stall_contract_is_pure_and_routes_its_handler() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text("utf-8"))
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    handler = contract["handler"]
    module = importlib.import_module(handler["module"])
    cls = getattr(module, handler["class"])
    assert callable(cls().handle)
    model_path, _, model_name = contract["input_model"].rpartition(".")
    assert getattr(importlib.import_module(model_path), model_name)
    topics = contract["event_bus"]
    assert topics["subscribe_topics"] == [contract["runtime_dispatch"]["command_topic"]]
    assert topics["publish_topics"] == [contract["terminal_event"]]
    assert contract["terminal_event"] == "onex.evt.omnimarket.landing-stall-folded.v1"
    produced = [t["topic"] for t in contract["externally_produced_topics"]]
    assert produced == ["onex.evt.omnimarket.landing-stall-transition.v1"]

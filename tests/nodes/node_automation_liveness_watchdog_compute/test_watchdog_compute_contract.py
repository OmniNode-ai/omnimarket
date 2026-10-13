# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watchdog compute's contract names a pure node and its handler (OMN-20806)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_automation_liveness_watchdog_compute"
)


def test_watchdog_compute_contract_is_pure_and_routes_its_handler() -> None:
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
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.automation-liveness-watchdog-evaluated.v1"
    )


def test_watchdog_compute_handler_imports_no_io() -> None:
    forbidden = ("subprocess", "socket", "requests", "httpx", "aiokafka", "open(")
    for path in (NODE / "handlers").glob("*.py"):
        text = path.read_text("utf-8")
        assert not any(f"import {name}" in text for name in forbidden[:-1]), path
        assert "datetime.now" not in text, path
        assert "utcnow" not in text, path

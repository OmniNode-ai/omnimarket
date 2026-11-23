# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The running-read due node is declared, registered and wired into the landing workflow (OMN-26491)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
NODE = "node_running_read_due_compute"


def _contract() -> dict[str, object]:
    path = ROOT / "src" / "omnimarket" / "nodes" / NODE / "contract.yaml"
    return yaml.safe_load(path.read_text())


def test_contract_declares_both_operations_and_its_topics() -> None:
    contract = _contract()
    routing = contract["handler_routing"]
    assert isinstance(routing, dict)
    operations = {entry["operation"] for entry in routing["handlers"]}
    assert operations == {"write_due_row", "read_due_rows"}
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    # Direct invocation: a declared topic would owe a broker permission row in another repository.
    assert bus["subscribe_topics"] == []
    assert bus["publish_topics"] == []


def test_handler_named_by_the_contract_imports() -> None:
    contract = _contract()
    handler = contract["handler"]
    assert isinstance(handler, dict)
    module = importlib.import_module(str(handler["module"]))
    assert hasattr(module, str(handler["class"]))


def test_entry_point_is_registered_in_pyproject() -> None:
    text = (ROOT / "pyproject.toml").read_text()
    assert f'{NODE} = "omnimarket.nodes.{NODE}"' in text


def test_landing_workflow_lists_the_step() -> None:
    path = (
        ROOT
        / "src"
        / "omnimarket"
        / "contracts"
        / "landing_workflow"
        / "landing_workflow.v1.yaml"
    )
    steps = yaml.safe_load(path.read_text())["steps"]
    step = next(item for item in steps if item["id"] == "running_read_due")
    assert step["node"] == NODE
    assert "command_topic" not in step

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invoke the packaged contract with a typed request (OMN-21409)."""

from __future__ import annotations

import importlib
from dataclasses import fields, is_dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml

pytestmark = pytest.mark.unit

PACKAGE = "omnimarket.nodes.node_idle_slot_fill_effect"


def test_contract_golden_chain() -> None:
    package = importlib.import_module(PACKAGE)
    assert package.__file__ is not None
    root = Path(package.__file__).parent
    contract = yaml.safe_load((root / "contract.yaml").read_text(encoding="utf-8"))
    metadata = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))
    assert contract["name"] == metadata["name"] == "node_idle_slot_fill_effect"
    assert contract["lifecycle"] == "experimental"
    assert metadata["pack"] == "distributed_validation"
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )

    def model(ref: str) -> Any:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert is_dataclass(result)
        assert result.__dataclass_params__.frozen
        return result

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class
    operations = {
        row["operation"]: row["handler"]["name"]
        for row in contract["handler_routing"]["handlers"]
    }
    assert operations == {
        "fill": "HandlerIdleSlotFill",
        "turn-guard": "HandlerIdleTurnGuard",
    }
    assert "event_bus" not in contract
    assert [row["topic"] for row in contract["handler_routing"]["handlers"]] == [
        "onex.cmd.omnimarket.idle-slot-fill-requested.v1",
        "onex.cmd.omnimarket.idle-turn-guard-requested.v1",
    ]
    assert contract["runtime_dispatch"]["command_topic"] == (
        "onex.cmd.omnimarket.idle-slot-fill-requested.v1"
    )
    assert contract["terminal_event"] == (
        "onex.evt.omnimarket.idle-slot-fill-completed.v1"
    )
    node_class = importlib.import_module(f"{PACKAGE}.node").NodeIdleSlotFillEffect
    assert node_class.contract_path == root / "contract.yaml"
    result = node_class().handle(
        request_class(
            now="2026-10-18T21:30:00Z",
            hosts=[],
            lines=[],
        )
    )
    assert isinstance(result, result_class)
    assert result.sends == []
    # OMN-21475 (T-S3): the result carries the count of items that failed ModelBacklogItem.
    assert "backlog_mismatched" in {f.name for f in fields(result_class)}
    assert result.backlog_mismatched == 0


def test_the_node_is_an_onex_nodes_entry_point() -> None:
    names = {e.name for e in entry_points(group="onex.nodes")}
    assert "node_idle_slot_fill_effect" in names

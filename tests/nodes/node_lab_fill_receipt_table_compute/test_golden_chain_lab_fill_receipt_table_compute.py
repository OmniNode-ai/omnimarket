# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the packaged receipt table contract and run its handler through it."""

from __future__ import annotations

import importlib
from importlib.resources import files

import pytest
import yaml

pytestmark = pytest.mark.unit

NAME = "node_lab_fill_receipt_table_compute"
COMMAND = "onex.cmd.omnimarket.lab-fill-receipt-table-requested.v1"
TERMINAL = "onex.evt.omnimarket.lab-fill-receipt-table-decided.v1"


def test_golden_chain_lab_fill_receipt_table_compute() -> None:
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL]
    assert contract["terminal_event"] == TERMINAL

    (entry,) = contract["handler_routing"]["handlers"]
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    input_module, _, input_name = entry["input_model"].rpartition(".")
    output_module, _, output_name = entry["output_model"].rpartition(".")
    request_type = getattr(importlib.import_module(input_module), input_name)
    result_type = getattr(importlib.import_module(output_module), output_name)

    result = handler_type().handle(request_type(lanes=()))
    assert isinstance(result, result_type)
    assert result.rows == ()
    assert "no lanes" in result.table

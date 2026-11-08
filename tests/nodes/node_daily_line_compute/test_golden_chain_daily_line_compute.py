# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the packaged daily line contract and run its handler through it (OMN-22925), golden and error leg."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

NAME = "node_daily_line_compute"
COMMAND = "onex.cmd.omnimarket.daily-line-compute-requested.v1"
TERMINAL = "onex.evt.omnimarket.daily-line-computed.v1"


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    assert isinstance(loaded, dict)
    return loaded


def _routed() -> tuple[Any, Any, Any]:
    contract = _contract()
    (entry,) = contract["handler_routing"]["handlers"]
    handler_type = getattr(
        importlib.import_module(entry["handler"]["module"]), entry["handler"]["name"]
    )
    request_module, _, request_name = entry["input_model"].rpartition(".")
    result_module, _, result_name = entry["output_model"].rpartition(".")
    return (
        handler_type,
        getattr(importlib.import_module(request_module), request_name),
        getattr(importlib.import_module(result_module), result_name),
    )


def test_golden_chain_daily_line_compute() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"] == {"command_topic": COMMAND}
    assert contract["terminal_event"] == TERMINAL

    handler_type, request_type, result_type = _routed()
    piece_module = importlib.import_module("omnimarket.models.model_daily_line")
    result = handler_type().handle(
        request_type(
            piece1=piece_module.ModelDailyLinePiece(state="WORKING", detail="ok"),
            j1_state="passed",
            open_claims=3,
        )
    )
    assert isinstance(result, result_type)
    assert result.line.endswith("by J1: passed; claims with no TERMINAL: 3.")
    assert result.unknown_parts == ()


def test_error_chain_daily_line_compute_unread_sources_render_unknown() -> None:
    handler_type, request_type, result_type = _routed()
    result = handler_type().handle(request_type())
    assert isinstance(result, result_type)
    assert result.unknown_parts == ("piece1", "j1", "claims")
    assert result.line.endswith("by J1: UNKNOWN; claims with no TERMINAL: UNKNOWN.")

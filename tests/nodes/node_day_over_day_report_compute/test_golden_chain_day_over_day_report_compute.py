# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the packaged day-over-day contract and run its handler through it (OMN-26212), golden and error leg."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

NAME = "node_day_over_day_report_compute"
COMMAND = "onex.cmd.omnimarket.day-over-day-report-compute-requested.v1"
TERMINAL = "onex.evt.omnimarket.day-over-day-report-computed.v1"


def _contract() -> dict[str, Any]:
    loaded = yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )
    assert isinstance(loaded, dict)
    return loaded


def _routed() -> tuple[Any, Any, Any]:
    (entry,) = _contract()["handler_routing"]["handlers"]
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


def test_golden_chain_day_over_day_report_compute() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["runtime_dispatch"] == {"command_topic": COMMAND}
    assert contract["terminal_event"] == TERMINAL
    handler_type, request_type, result_type = _routed()
    models = importlib.import_module("omnimarket.models.model_day_over_day_report")
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    result = handler_type().handle(
        request_type(
            yesterday=models.ModelDayReadings(label="2026-11-19"),
            today=models.ModelDayReadings(label="2026-11-20"),
            as_of=now,
        )
    )
    assert isinstance(result, result_type)


def test_error_leg_unread_sources_read_unreadable() -> None:
    handler_type, request_type, _ = _routed()
    models = importlib.import_module("omnimarket.models.model_day_over_day_report")
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    result = handler_type().handle(
        request_type(
            yesterday=models.ModelDayReadings(label="2026-11-19"),
            today=models.ModelDayReadings(label="2026-11-20"),
            as_of=now,
        )
    )
    assert result.newly_running.reading == models.UNREADABLE
    assert result.throughput.merges_today.value == models.UNREADABLE

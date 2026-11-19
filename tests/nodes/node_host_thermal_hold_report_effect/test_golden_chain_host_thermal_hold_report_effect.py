# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve and execute the packaged host thermal hold report contract over in-memory ports and over real guard files."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_thermal_hold_report_effect.adapters import (
    FileEpisodeStore,
    GuardFileState,
    InMemoryLedgerAppender,
)

from .conftest import LAST, LATCH, NODE, NOW, outbox_row, present

pytestmark = pytest.mark.unit


def _contract() -> dict:
    return yaml.safe_load(files(NODE).joinpath("contract.yaml").read_text())


def _types():
    from omnimarket.nodes.node_host_thermal_hold_report_effect.handlers import (
        HandlerHostThermalHoldReport,
    )
    from omnimarket.nodes.node_host_thermal_hold_report_effect.models import (
        ModelHostThermalHoldReportRequest,
        ModelHostThermalHoldReportResult,
    )

    return (
        HandlerHostThermalHoldReport,
        ModelHostThermalHoldReportRequest,
        ModelHostThermalHoldReportResult,
    )


def test_the_contract_declares_one_report_operation_and_its_terminal_event() -> None:
    contract = _contract()
    assert contract["name"] == "node_host_thermal_hold_report_effect"
    assert contract["node_type"] == "effect"
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.host-thermal-hold-reported.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    # The handler takes its ports at construction, so the runtime cannot boot-resolve it: no handler binding.
    assert "handler_routing" not in contract
    assert "handler" not in contract
    assert "handlers" not in contract


def test_golden_run_over_the_in_memory_ports(
    handler, guard, episodes, appender, request_for
) -> None:
    handler_type, request_type, result_type = _types()
    assert isinstance(handler, handler_type)
    guard.latch = present(LATCH)
    guard.outbox = [outbox_row(1), outbox_row(2)]
    request = request_for()
    assert isinstance(request, request_type)
    result = handler.handle(request)
    assert isinstance(result, result_type)
    assert (result.alarm, result.delivered, result.kept, result.ok) == (
        "trip",
        2,
        0,
        True,
    )
    assert len(appender.sent) == 3
    assert episodes.record["open"] is True


def test_golden_run_over_real_guard_files(tmp_path: Path, overlay, request_for) -> None:
    from omnimarket.nodes.node_host_thermal_hold_report_effect.handlers import (
        HandlerHostThermalHoldReport,
    )

    state = tmp_path / "guard"
    state.mkdir()
    (state / overlay.latch_file).write_text(json.dumps(LATCH))
    (state / overlay.last_file).write_text(json.dumps(LAST))
    (state / overlay.outbox_file).write_text(
        outbox_row(1) + "\n" + outbox_row(2) + "\n"
    )
    appender = InMemoryLedgerAppender()
    handler = HandlerHostThermalHoldReport(
        GuardFileState(
            state,
            overlay.latch_file,
            overlay.last_file,
            overlay.outbox_file,
            overlay.refused_file,
            overlay.claim_suffix,
        ),
        FileEpisodeStore(tmp_path / "unit", overlay.episode_file),
        appender,
        lambda: NOW,
    )
    result = handler.handle(request_for())
    assert result.ok
    assert result.alarm == "trip"
    assert result.delivered == 2
    assert not (state / overlay.outbox_file).exists()
    assert not (state / f"{overlay.outbox_file}{overlay.claim_suffix}").exists()
    assert (
        json.loads((tmp_path / "unit" / overlay.episode_file).read_text())["open"]
        is True
    )
    assert (
        state / overlay.latch_file
    ).is_file()  # the node never touches the guard's latch

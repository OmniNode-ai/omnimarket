# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lost dispatch clears when a later lineage receipt ended in a terminal state."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit


def dispatch_lines(receipts: list[dict[str, Any]], now: str) -> list[str]:
    result = HandlerThroughputTickDecision().handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": now,
                "launchctl": "unavailable",
                "dispatch_window_hours": 3,
                "lab_headroom": {"hosts": [], "receipts": receipts},
            }
        )
    )
    assert "dispatches" in result.checked
    return [line for line in result.lines if line.startswith("MISSING dispatch:")]


def receipt(lane: str | None, **changes: Any) -> dict[str, Any]:
    return {
        "lane": lane,
        "started_at": "2026-11-02T16:00:00Z",
        "status": "failed",
        "brief": f"/briefs/{lane}.md",
        **changes,
    }


def test_dispatch_lost_later_terminal_cf_22213_1102c() -> None:
    lost = receipt(
        "cf-22213-1102c",
        run_id="rlane-cf-22213-1102c-53317c",
        started_at="2026-11-03T02:22:15Z",
        status="no-host",
    )
    again = receipt(
        "cf-22213-1102c",
        run_id="rlane-cf-22213-1102c-31a85e",
        started_at="2026-11-03T02:24:46Z",
        status="cloud-no-pr",
    )
    assert dispatch_lines([lost, again], now="2026-11-03T03:00:00Z") == []


def test_dispatch_lost_later_terminal_pinwire_spi_1745() -> None:
    lost = receipt(
        "pinwire-spi-1745",
        run_id="rlane-pinwire-spi-1745-96bda2",
        started_at="2026-11-03T17:56:00Z",
        status="no-host",
    )
    again = receipt(
        "pinwire-spi-1745",
        run_id="rlane-pinwire-spi-1745-112827",
        started_at="2026-11-03T18:35:00Z",
        status="blocked",
        host="h053",
    )
    assert dispatch_lines([lost, again], now="2026-11-03T19:09:00Z") == []


@pytest.mark.parametrize("status", ["blocked", "cloud-no-pr", "handed-off", "partial"])
def test_dispatch_lost_later_terminal_statuses(status: str) -> None:
    lost = receipt(
        "pinwire-spi-1745",
        ticket="ticket-a",
        started_at="2026-11-03T17:56:00Z",
        status="no-host",
    )
    again = receipt(
        "replacement",
        ticket="ticket-a",
        started_at="2026-11-03T18:35:00Z",
        status=status,
    )
    assert len(dispatch_lines([lost], now="2026-11-03T19:09:00Z")) == 1
    assert dispatch_lines([lost, again], now="2026-11-03T19:09:00Z") == []


def test_dispatch_lost_still_missing_without_later_run() -> None:
    lost = receipt(
        "pinwire-spi-1745",
        run_id="rlane-pinwire-spi-1745-96bda2",
        started_at="2026-11-03T17:56:00Z",
        status="no-host",
    )
    lines = dispatch_lines([lost], now="2026-11-03T19:09:00Z")
    assert len(lines) == 1
    assert lines[0].startswith("MISSING dispatch:pinwire-spi-1745 |")


@pytest.mark.parametrize("status", ["failed", "no-host", "placing", "host-limited"])
def test_dispatch_lost_still_missing_when_later_never_ran(status: str) -> None:
    lost = receipt(
        "pinwire-spi-1745",
        run_id="rlane-pinwire-spi-1745-96bda2",
        started_at="2026-11-03T17:56:00Z",
        status="no-host",
    )
    later = receipt(
        "pinwire-spi-1745",
        started_at="2026-11-03T18:35:00Z",
        status=status,
    )
    lines = dispatch_lines([lost, later], now="2026-11-03T19:09:00Z")
    assert any(line.startswith("MISSING dispatch:pinwire-spi-1745 |") for line in lines)


def test_dispatch_lost_still_missing_earlier_terminal() -> None:
    lost = receipt(
        "pinwire-spi-1745",
        run_id="rlane-pinwire-spi-1745-96bda2",
        started_at="2026-11-03T17:56:00Z",
        status="no-host",
    )
    earlier = receipt(
        "pinwire-spi-1745",
        run_id="rlane-pinwire-spi-1745-112827",
        started_at="2026-11-03T17:20:00Z",
        status="blocked",
        host="h053",
    )
    lines = dispatch_lines([earlier, lost], now="2026-11-03T19:09:00Z")
    assert any(line.startswith("MISSING dispatch:pinwire-spi-1745 |") for line in lines)

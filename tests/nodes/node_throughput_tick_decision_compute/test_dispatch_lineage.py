# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Dispatch findings follow a ticket or an exact relaunch lane family."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "recorded_relaunch.json"


def dispatch_lines(receipts: list[dict[str, Any]]) -> list[str]:
    result = HandlerThroughputTickDecision().handle(
        ModelThroughputTickRequest.model_validate(
            {
                "now": "2026-11-02T18:00:00Z",
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


@pytest.mark.parametrize("status", ["running", "done"])
@pytest.mark.parametrize(
    ("earlier", "later"),
    [
        ("readers-node-1102", "readers-node-1102-r"),
        ("host-declare-resume-1102", "host-declare-resume-1102-r2"),
        ("comp-cm11-1102-r3", "comp-cm11-1102-r4"),
        ("repair-r2", "repair"),
        ("repair-r", "repair-r3"),
        ("repair", "repair"),
    ],
)
def test_lineage_relaunch_lane_clears_failed_dispatch(
    earlier: str, later: str, status: str
) -> None:
    failed = receipt(earlier)
    again = receipt(later, started_at="2026-11-02T17:00:00Z", status=status)
    assert len(dispatch_lines([failed])) == 1
    assert dispatch_lines([failed, again]) == []


@pytest.mark.parametrize("status", ["running", "done"])
def test_lineage_same_ticket_with_different_lane_and_brief(status: str) -> None:
    failed = receipt("repair", ticket="ticket-a")
    again = receipt(
        "replacement",
        ticket="ticket-a",
        started_at="2026-11-02T17:00:00Z",
        status=status,
    )
    assert len(dispatch_lines([failed])) == 1
    assert dispatch_lines([failed, again]) == []


@pytest.mark.parametrize(
    "later", ["repair-random", "repair-r2-extra", "repair-extra-r2", "other"]
)
def test_lineage_does_not_match_unrelated_lane_prefixes(later: str) -> None:
    lines = dispatch_lines(
        [
            receipt("repair"),
            receipt(later, started_at="2026-11-02T17:00:00Z", status="running"),
        ]
    )
    assert len(lines) == 1
    assert lines[0].startswith("MISSING dispatch:repair |")


@pytest.mark.parametrize("ticket", [None, "", "ticket-b"])
def test_lineage_absent_empty_or_different_ticket_does_not_clear(
    ticket: str | None,
) -> None:
    lines = dispatch_lines(
        [
            receipt("repair", ticket="ticket-a" if ticket else ticket),
            receipt(
                "other",
                ticket="ticket-b" if ticket else ticket,
                started_at="2026-11-02T17:00:00Z",
                status="running",
            ),
        ]
    )
    assert len(lines) == 1
    assert lines[0].startswith("MISSING dispatch:repair |")


@pytest.mark.parametrize(
    ("started_at", "status"),
    [
        ("2026-11-02T15:59:59Z", "running"),
        ("2026-11-02T16:00:00Z", "done"),
        ("invalid", "running"),
        ("2026-11-02T17:00:00Z", "placing"),
        ("2026-11-02T17:00:00Z", "failed"),
    ],
)
def test_lineage_requires_a_later_running_or_done_receipt(
    started_at: str, status: str
) -> None:
    lines = dispatch_lines(
        [
            receipt("repair", ticket="ticket-a"),
            receipt(
                "repair-r2", ticket="ticket-a", started_at=started_at, status=status
            ),
        ]
    )
    assert any(line.startswith("MISSING dispatch:repair |") for line in lines)


@pytest.mark.parametrize("with_ticket", [True, False])
def test_recorded_relaunch_clears_failure_but_preserves_unrelated_ticket(
    with_ticket: bool,
) -> None:
    # Field allowlist from the recorded r3/r4 receipts: private paths are reduced
    # to basenames, ticket values are anonymized, and all other fields omitted.
    recorded: list[dict[str, Any]] = json.loads(FIXTURE.read_text())
    if not with_ticket:
        for item in recorded:
            item.pop("ticket")
    failed, running = recorded
    assert failed["lane"] == "comp-cm11-1102-r3"
    assert running["lane"] == "comp-cm11-1102-r4"
    assert len(dispatch_lines([failed])) == 1
    unrelated = receipt("unrelated", ticket="another-ticket")
    if not with_ticket:
        unrelated.pop("ticket")
    lines = dispatch_lines([failed, running, unrelated])
    assert len(lines) == 1
    assert lines[0].startswith("MISSING dispatch:unrelated |")

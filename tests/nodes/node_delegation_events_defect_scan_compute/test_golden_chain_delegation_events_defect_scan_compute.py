# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain (OMN-23331): the packaged contract routes the scan operation to the handler, a clean day reads WORKING and its row
carries the three counts; each named mutation turns the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule in the handler module and runs the same golden assertions; the mutated chain
must raise AssertionError.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_delegation_events_defect_scan_compute.handlers import (
    handler_delegation_events_defect_scan as module,
)
from tests.nodes.node_delegation_events_defect_scan_compute.defect_scan_support import (
    NOW,
    clean_day,
    contract,
    overlay,
    request,
    resolve_handler,
)

pytestmark = pytest.mark.unit

NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "clean-day-reads-not-working": (
        "golden",
        "test_mutation_clean_day_reads_not_working_turns_the_golden_chain_red",
    ),
    "row-drops-the-counts": (
        "golden",
        "test_mutation_row_drops_the_counts_turns_the_golden_chain_red",
    ),
    "window-edge-counts-the-old-row": (
        "golden",
        "test_mutation_window_edge_counts_the_old_row_turns_the_golden_chain_red",
    ),
}


def run_golden_chain() -> None:
    declared = contract()
    assert declared["node_type"] == "compute"
    assert declared["descriptor"]["side_effects"] == []
    assert declared["handler_routing"]["handlers"][0]["operation"] == (
        "delegation_events_defect_scan"
    )
    assert declared["terminal_event"] in declared["event_bus"]["publish_topics"]
    scan = resolve_handler()().handle(request())
    assert scan.verdict is V.WORKING
    assert scan.rows_scanned == 120
    for cell in ("missing_terminal=0", "cost_unread_as_zero=0", "unattributed=0"):
        assert cell in scan.ledger_row
    assert "rows=120" in scan.ledger_row
    assert "verdict=WORKING" in scan.ledger_row
    edge = clean_day()[-1].model_copy(
        update={"event_time": NOW - timedelta(seconds=overlay().window_seconds)}
    )
    at_the_edge = resolve_handler()().handle(request(events=(*clean_day(), edge)))
    assert at_the_edge.rows_scanned == 120


def test_delegation_events_defect_scan_golden_chain_clean_day() -> None:
    run_golden_chain()


def test_mutation_clean_day_reads_not_working_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "judge", lambda *_a: ["forced"])
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_row_drops_the_counts_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.ledger_row

    def mutated(*args: Any) -> str:
        return real(*args).replace("unattributed=", "u=")

    monkeypatch.setattr(module, "ledger_row", mutated)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_window_edge_counts_the_old_row_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.in_window

    def mutated(req: Any) -> Any:
        widened = req.model_copy(
            update={"overlay": req.overlay.model_copy(update={"window_seconds": 90000})}
        )
        return real(widened)

    monkeypatch.setattr(module, "in_window", mutated)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_the_golden_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_golden_chain()


def test_every_named_golden_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names

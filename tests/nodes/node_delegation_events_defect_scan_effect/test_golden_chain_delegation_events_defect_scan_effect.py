# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain (OMN-23331): a clean day is read, the node's row is appended once and read back from the ledger, with the start and
terminal events the contract declares; each named mutation turns the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule in the handler module and runs the same golden assertions; the mutated chain
must raise AssertionError.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta
from importlib.resources import files
from typing import Any

import pytest
import yaml

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_delegation_events_defect_scan_effect.handlers import (
    handler_delegation_events_defect_scan_run as module,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.models.model_defect_scan_run import (
    ModelDefectScanCommand,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerQuery
from tests.nodes.node_delegation_events_defect_scan_compute.defect_scan_support import (
    NOW,
)
from tests.nodes.node_delegation_events_defect_scan_effect.defect_scan_run_support import (
    command,
    handler,
)

pytestmark = pytest.mark.unit

NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "probe-writes-a-row": (
        "golden",
        "test_mutation_probe_writes_a_row_turns_the_golden_chain_red",
    ),
    "retry-appends-twice": (
        "golden",
        "test_mutation_retry_appends_twice_turns_the_golden_chain_red",
    ),
    "terminal-event-on-the-wrong-topic": (
        "golden",
        "test_mutation_terminal_event_on_the_wrong_topic_turns_the_golden_chain_red",
    ),
}


def contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        files("omnimarket.nodes.node_delegation_events_defect_scan_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    return loaded


def stored_rows(ledger: Any) -> list[str]:
    read = ledger.read(
        ModelLedgerQuery(
            since=NOW - timedelta(days=1),
            until=NOW + timedelta(days=1),
            row_type="STATUS",
            lane="delegation-defect-scan",
        )
    )
    assert read.error is None
    return [row.raw for row in read.rows]


def run_golden_chain() -> None:
    """The golden assertions: contract topics, one good run, a retry of it, and a probe."""
    topics = contract()["topics"]
    assert contract()["terminal_event"] == topics["defect_scan_run_completed"]
    assert (
        topics["defect_scan_run_completed"]
        == "onex.evt.omnimarket.delegation-events-defect-scan-run-completed.v1"
    )
    run, ledger = handler()
    result = run.handle(command())
    assert result.status == "read"
    assert result.verdict is V.WORKING
    assert (
        result.missing_terminal,
        result.cost_unread_as_zero,
        result.unattributed,
    ) == (0, 0, 0)
    assert result.rows_scanned == 120
    assert result.ledger == "written"
    assert [topic for topic, _ in result.events] == [
        topics["defect_scan_run_started"],
        topics["defect_scan_run_completed"],
    ]
    (row,) = stored_rows(ledger)
    assert "verdict=WORKING" in row
    assert "rows=120" in row
    again = run.handle(command(correlation_id="scan-run-2"))
    assert again.ledger == "already-written"
    assert len(stored_rows(ledger)) == 1
    probe_run, probe_ledger = handler()
    probed = probe_run.handle(command(write=False))
    assert probed.ledger == "not-written"
    assert probed.verdict is V.WORKING
    assert stored_rows(probe_ledger) == []


def test_delegation_events_defect_scan_contract_declares_an_effect_that_writes_a_ledger_row_and_nothing_else() -> (
    None
):
    declared = contract()
    assert declared["name"] == "node_delegation_events_defect_scan_effect"
    assert declared["node_type"] == "effect"
    assert declared["descriptor"]["side_effects"] == [
        "ledger-append",
        "postgres-read-over-ssh",
    ]
    assert declared["overlay"]["node"] == "node_delegation_events_defect_scan_effect"
    assert declared["schedule"]["cadence"]["on_calendar"].startswith("*-*-*")


def test_delegation_events_defect_scan_golden_chain_a_clean_day_is_read_and_its_row_appended_once() -> (
    None
):
    run_golden_chain()


def test_delegation_events_defect_scan_the_row_written_is_a_status_row_of_the_scan_lane() -> (
    None
):
    run, ledger = handler()
    run.handle(command())
    (row,) = stored_rows(ledger)
    assert row.startswith(
        "2026-11-09T06:15:00Z | STATUS | lane=delegation-defect-scan | src=delegation-defect-scan | drill=no |"
    )


def _probe_writes(real: Callable[..., Any]) -> Callable[..., Any]:
    def mutated(self: Any, cmd: ModelDefectScanCommand) -> Any:
        return real(self, cmd.model_copy(update={"write": True}))

    return mutated


def test_mutation_probe_writes_a_row_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module.HandlerDelegationEventsDefectScanRun,
        "handle",
        _probe_writes(module.HandlerDelegationEventsDefectScanRun.handle),
    )
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_retry_appends_twice_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uuid, "uuid5", lambda *_a: uuid.uuid4())
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_terminal_event_on_the_wrong_topic_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.load_topics

    def mutated() -> dict[str, str]:
        topics = real()
        topics["defect_scan_run_completed"] = topics["defect_scan_run_failed"]
        return topics

    monkeypatch.setattr(module, "load_topics", mutated)
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_the_golden_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_golden_chain()


def test_every_named_golden_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"golden"}

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain (OMN-23331): rows that cannot be read write an UNKNOWN row and never a zero, a day with a planted defect still writes
its row as NOT WORKING, a row that cannot be appended is a failed run, and each named mutation turns the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule and runs the same error assertions; the mutated chain must raise AssertionError.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_delegation_events_defect_scan_effect.adapters import (
    events_source,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.handlers import (
    handler_delegation_events_defect_scan_run as module,
)
from omnimarket.nodes.node_delegation_events_defect_scan_effect.models.model_defect_scan_run import (
    ModelDefectScanCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_delegation_events_defect_scan_compute.defect_scan_support import (
    planted,
)
from tests.nodes.node_delegation_events_defect_scan_effect.defect_scan_run_support import (
    command,
    handler,
)
from tests.nodes.node_delegation_events_defect_scan_effect.test_golden_chain_delegation_events_defect_scan_effect import (
    contract,
    stored_rows,
)

pytestmark = pytest.mark.unit

NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "unreadable-source-read-as-empty": (
        "error",
        "test_mutation_unreadable_source_read_as_empty_turns_the_error_chain_red",
    ),
    "append-failure-swallowed": (
        "error",
        "test_mutation_append_failure_swallowed_turns_the_error_chain_red",
    ),
    "failed-run-ends-on-the-completed-topic": (
        "error",
        "test_mutation_failed_run_ends_on_the_completed_topic_turns_the_error_chain_red",
    ),
}


def run_error_chain() -> None:
    topics = contract()["topics"]
    unread, unread_ledger = handler(error="rows-read-timeout")
    result = unread.handle(command())
    assert result.status == "read"
    assert result.verdict is V.UNKNOWN
    assert (
        result.missing_terminal,
        result.cost_unread_as_zero,
        result.unattributed,
    ) == (None, None, None)
    assert result.reasons == ("events-unreadable:rows-read-timeout",)
    (row,) = stored_rows(unread_ledger)
    assert "outcome=source-unreadable" in row
    assert "verdict=UNKNOWN" in row

    defective, defective_ledger = handler(events=planted("missing_terminal", 2))
    red = defective.handle(command())
    assert red.status == "read"
    assert red.verdict is V.NOT_WORKING
    assert red.missing_terminal == 2
    assert red.ledger == "written"
    (bad_row,) = stored_rows(defective_ledger)
    assert "verdict=NOT_WORKING" in bad_row
    assert "missing_terminal=2" in bad_row
    assert "outcome=read" in bad_row

    refusing = InMemoryLedger()
    refusing.append_status = "refused"
    failing, _ = handler(ledger=refusing)
    failed = failing.handle(command())
    assert failed.status == "failed"
    assert failed.ledger == "append-failed:refused"
    assert failed.failure is not None
    assert failed.failure.startswith("append-failed:refused")
    assert failed.events[-1][0] == topics["defect_scan_run_failed"]
    assert stored_rows(refusing) == []

    no_appender, _ = handler(write=False)
    refused = no_appender.handle(command(write=True))
    assert refused.status == "failed"
    assert refused.failure == "no-appender-for-a-writing-run"
    assert refused.events[-1][0] == topics["defect_scan_run_failed"]


def test_delegation_events_defect_scan_error_chain_unreadable_rows_and_a_refused_append_end_by_name() -> (
    None
):
    run_error_chain()


def test_delegation_events_defect_scan_error_chain_a_well_formed_run_is_written() -> (
    None
):
    """Positive control: the failing runs differ from this one in one input each."""
    run, ledger = handler()
    assert run.handle(command()).ledger == "written"
    assert len(stored_rows(ledger)) == 1


def _empty_when_unread() -> Callable[..., Any]:
    real = events_source.InMemoryDefectEventSource.read

    def mutated(self: Any, window_seconds: int) -> Any:
        read = real(self, window_seconds)
        return read.model_copy(update={"error": None, "now": read.now})

    return mutated


def test_mutation_unreadable_source_read_as_empty_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        events_source.InMemoryDefectEventSource, "read", _empty_when_unread()
    )
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_append_failure_swallowed_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.HandlerDelegationEventsDefectScanRun.handle

    def mutated(self: Any, cmd: ModelDefectScanCommand) -> Any:
        result = real(self, cmd)
        if result.failure and result.failure.startswith("append-failed"):
            return result.model_copy(update={"status": "read", "failure": None})
        return result

    monkeypatch.setattr(module.HandlerDelegationEventsDefectScanRun, "handle", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_failed_run_ends_on_the_completed_topic_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.load_topics

    def mutated() -> dict[str, str]:
        topics = real()
        topics["defect_scan_run_failed"] = topics["defect_scan_run_completed"]
        return topics

    monkeypatch.setattr(module, "load_topics", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_the_error_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_error_chain()


def test_every_named_error_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"error"}

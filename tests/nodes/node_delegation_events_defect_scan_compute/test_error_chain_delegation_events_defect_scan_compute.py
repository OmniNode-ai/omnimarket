# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain (OMN-23331): a day with a planted defect, an empty day and a day of only shadow rows read NOT WORKING, rows that could
not be read read UNKNOWN with no count, a request the node cannot read as asked is refused by name, and each named mutation turns
the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule in the handler module and runs the same error assertions; the mutated chain
must raise AssertionError.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_delegation_events_defect_scan import (
    ModelDefectScanRequest,
)
from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_delegation_events_defect_scan_compute.handlers import (
    handler_delegation_events_defect_scan as module,
)
from tests.nodes.node_delegation_events_defect_scan_compute.defect_scan_support import (
    NOW,
    fact,
    overlay_data,
    planted,
    request,
    resolve_handler,
)

pytestmark = pytest.mark.unit

NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "missing-terminal-ignored": (
        "error",
        "test_mutation_missing_terminal_ignored_turns_the_error_chain_red",
    ),
    "unread-cost-ignored": (
        "error",
        "test_mutation_unread_cost_ignored_turns_the_error_chain_red",
    ),
    "unattributed-ignored": (
        "error",
        "test_mutation_unattributed_ignored_turns_the_error_chain_red",
    ),
    "empty-day-passes": (
        "error",
        "test_mutation_empty_day_passes_turns_the_error_chain_red",
    ),
    "unreadable-rows-read-as-zero-defects": (
        "error",
        "test_mutation_unreadable_rows_read_as_zero_defects_turns_the_error_chain_red",
    ),
    "shadow-rows-judged": (
        "error",
        "test_mutation_shadow_rows_judged_turns_the_error_chain_red",
    ),
}


def run_error_chain() -> None:
    handler = resolve_handler()()
    broken: dict[str, dict[str, Any]] = {
        "missing-terminal": {"events": planted("missing_terminal")},
        "cost-unread-as-zero": {"events": planted("cost_unread_as_zero")},
        "unattributed": {"events": planted("unattributed")},
        "empty-day": {"events": ()},
        "only-shadow-rows": {
            "events": tuple(fact(60 + i, shadow=True) for i in range(80))
        },
    }
    for name, edits in broken.items():
        scan = handler.handle(request(**edits))
        assert scan.verdict is V.NOT_WORKING, name
        assert scan.reasons, name
    unreadable = handler.handle(
        request(events=(), events_unread_reason="delegation_events-down")
    )
    assert unreadable.verdict is V.UNKNOWN
    assert unreadable.missing_terminal is None
    assert unreadable.cost_unread_as_zero is None
    assert unreadable.unattributed is None
    assert "outcome=source-unreadable" in unreadable.ledger_row
    assert "missing_terminal=-" in unreadable.ledger_row
    shadow_only = handler.handle(
        request(events=tuple(fact(60 + i, shadow=True, ok=None) for i in range(80)))
    )
    assert shadow_only.rows_scanned == 0


def test_delegation_events_defect_scan_error_chain() -> None:
    run_error_chain()


def test_delegation_events_defect_scan_a_request_that_cannot_be_read_is_refused_by_name() -> (
    None
):
    with pytest.raises(ValidationError, match="cannot both be given"):
        request(events=(fact(),), events_unread_reason="down")
    with pytest.raises(ValidationError, match="names no reason"):
        request(events=(), events_unread_reason="  ")
    with pytest.raises(ValidationError, match="not UTC"):
        ModelDefectScanRequest.model_validate(
            {
                "now": NOW.astimezone(timezone(timedelta(hours=2))),
                "overlay": overlay_data(),
                "events": (),
                "events_unread_reason": None,
            }
        )
    with pytest.raises(ValidationError):
        request(overlay={k: v for k, v in overlay_data().items() if k != "bounds"})


def test_delegation_events_defect_scan_an_overlay_without_a_bound_is_refused() -> None:
    bounds = {
        k: v for k, v in overlay_data()["bounds"].items() if k != "max_unattributed"
    }
    with pytest.raises(ValidationError, match="max_unattributed"):
        request(overlay={**overlay_data(), "bounds": bounds})


def test_mutation_missing_terminal_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.count_defects

    def mutated(req: Any, rows: Any) -> tuple[int, int, int, int]:
        n, _m, u, a = real(req, rows)
        return n, 0, u, a

    monkeypatch.setattr(module, "count_defects", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_unread_cost_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.count_defects

    def mutated(req: Any, rows: Any) -> tuple[int, int, int, int]:
        n, m, _u, a = real(req, rows)
        return n, m, 0, a

    monkeypatch.setattr(module, "count_defects", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_unattributed_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.count_defects

    def mutated(req: Any, rows: Any) -> tuple[int, int, int, int]:
        n, m, u, _a = real(req, rows)
        return n, m, u, 0

    monkeypatch.setattr(module, "count_defects", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_empty_day_passes_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.judge

    def mutated(overlay: Any, scanned: int, *counts: int) -> list[str]:
        return [r for r in real(overlay, scanned, *counts) if "rows-scanned" not in r]

    monkeypatch.setattr(module, "judge", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_unreadable_rows_read_as_zero_defects_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.HandlerDelegationEventsDefectScan.handle

    def mutated(self: Any, req: ModelDefectScanRequest) -> Any:
        return real(self, req.model_copy(update={"events_unread_reason": None}))

    monkeypatch.setattr(module.HandlerDelegationEventsDefectScan, "handle", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_shadow_rows_judged_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.count_defects

    def mutated(req: Any, rows: Any) -> tuple[int, int, int, int]:
        return real(req, [r.model_copy(update={"is_shadow": False}) for r in rows])

    monkeypatch.setattr(module, "count_defects", mutated)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_the_error_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_error_chain()


def test_every_named_error_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names

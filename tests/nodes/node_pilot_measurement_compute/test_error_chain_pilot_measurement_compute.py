# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain (OMN-23611): each fault reads as its typed outcome, and each named mutation turns the chain red.

The faults are the plan's piece 16 failures: a pilot that starts without a frozen bar, a planted fact not recalled, an eval
run that ends with no result; and the unreadable source, which is never read as an empty answer.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_pilot_measurement_compute.handlers import (
    handler_pilot_measurement as module,
)
from tests.nodes.node_pilot_measurement_compute.pilot_support import (
    declaration,
    fact,
    finished,
    request,
    resolve_handler,
)

NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "no-result-ignored": (
        "error",
        "test_mutation_no_result_ignored_turns_the_error_chain_red",
    ),
    "unread-reads-working": (
        "error",
        "test_mutation_unread_reads_working_turns_the_error_chain_red",
    ),
    "gating-miss-demoted": (
        "error",
        "test_mutation_gating_miss_demoted_turns_the_error_chain_red",
    ),
}


def run_error_chain() -> None:
    """Each fault reads NOT_WORKING (or UNKNOWN for an unread source) with its typed reason."""
    handler = resolve_handler()()
    no_bar = handler.handle(request(facts=[fact(bar_hash=None)]))
    assert (no_bar.verdict, no_bar.reasons) == (
        V.NOT_WORKING,
        ("pilot-refused:memory:hash_missing",),
    )
    late = handler.handle(
        request(declarations=[declaration(deadline=3600)], facts=[fact()])
    )
    assert (late.verdict, late.reasons) == (V.NOT_WORKING, ("pilot-no-result:memory",))
    missed = handler.handle(request(facts=[finished(planted_score=2.0)]))
    assert (missed.verdict, missed.reasons) == (
        V.NOT_WORKING,
        ("pilot-bar-missed:memory:planted_score",),
    )
    unread = handler.handle(request(facts=[], facts_unread_reason="ledger down"))
    assert (unread.verdict, unread.reasons) == (
        V.UNKNOWN,
        ("facts-unread:ledger-down",),
    )


def test_error_chain_each_fault_reads_its_typed_outcome() -> None:
    run_error_chain()


def test_mutation_no_result_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real: Any = module.judge_pilot

    def never_late(
        checker: Any, declaration_: Any, fact_: Any, _elapsed: float, tally: Any
    ) -> None:
        real(checker, declaration_, fact_, 0.0, tally)

    monkeypatch.setattr(module, "judge_pilot", never_late)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_unread_reads_working_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = module.HandlerPilotMeasurement.handle

    def ignores_unread(self: Any, req: Any) -> Any:
        return real(self, req.model_copy(update={"facts_unread_reason": None}))

    monkeypatch.setattr(module.HandlerPilotMeasurement, "handle", ignores_unread)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_mutation_gating_miss_demoted_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real: Any = module.judge_pilot

    def demoted(
        checker: Any, declaration_: Any, fact_: Any, elapsed: float, tally: Any
    ) -> None:
        real(
            checker,
            declaration_.model_copy(update={"gating_metrics": ("accuracy_gain",)}),
            fact_,
            elapsed,
            tally,
        )

    monkeypatch.setattr(module, "judge_pilot", demoted)
    with pytest.raises(AssertionError):
        run_error_chain()


def test_the_error_chain_is_green_again_after_each_mutation_is_undone() -> None:
    run_error_chain()


def test_every_named_error_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"error"}

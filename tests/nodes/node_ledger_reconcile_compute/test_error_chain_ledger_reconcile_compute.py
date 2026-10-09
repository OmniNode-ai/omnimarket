# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: a command the node cannot decide is blocked and says why; nothing is read as clean (OMN-20677)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.ledger_reconcile import (
    ModelAppendOutcome,
    ModelPrFact,
    ModelPushFact,
    ModelReconcileDecision,
    ModelReconcileFacts,
    ModelReconcileParams,
    ModelReconcileRenderRequest,
)

from .support import ROWS, bindings, decide_request

MERGED = ModelPrFact(
    repo="omnimarket",
    number=11,
    state="MERGED",
    merged_at="2026-09-22T11:00:00Z",
    merge_sha="a" * 40,
    title="fix(OMN-1): claimed work",
)
OPEN = ModelPrFact(
    repo="omnimarket", number=12, state="OPEN", title="fix(OMN-1): claimed work"
)
FACTS = ModelReconcileFacts(
    prs=(MERGED, OPEN), pushes=(ModelPushFact(repo="omnimarket", number=12),)
)


def _decide(
    rows: list[str] | None = None,
    facts: ModelReconcileFacts | None = None,
    params: ModelReconcileParams | None = None,
) -> ModelReconcileDecision:
    handler = bindings()["decide_ledger_reconcile"][0]()
    decision = handler.handle(decide_request(rows, facts=facts, params=params))
    assert isinstance(decision, ModelReconcileDecision)
    return decision


@pytest.mark.parametrize(
    "params",
    [
        ModelReconcileParams(stale_hours=-1),
        ModelReconcileParams(since_days=-0.5),
        ModelReconcileParams(silent_hours=0, live_roster_known=True),
        ModelReconcileParams(silent_hours=-3, live_roster_known=True),
    ],
)
def test_negative_time_bounds_are_blocked(params: ModelReconcileParams) -> None:
    decision = _decide(params=params)
    assert decision.status == "blocked"
    assert "time bounds must be nonnegative" in decision.blocked_reason
    assert decision.planned == ()


def test_silent_retirement_without_a_supplied_roster_is_blocked() -> None:
    decision = _decide(params=ModelReconcileParams(silent_hours=72))
    assert decision.status == "blocked"
    assert "--silent-hours requires --live-lanes" in decision.blocked_reason


def test_a_ledger_that_holds_claims_the_parser_cannot_read_is_never_clean() -> None:
    unreadable = ["see the notes | CLAIM | for the lane, no stamp, no shape"]
    decision = _decide(rows=unreadable)
    assert decision.status == "blocked"
    assert decision.blocked_reason.startswith("POSITIVE CONTROL FAILED")
    assert "zero CLAIM rows parsed" in decision.blocked_reason


def test_a_blocked_decision_renders_as_exit_three_with_the_reason() -> None:
    request = decide_request(params=ModelReconcileParams(stale_hours=-1))
    result = bindings()["render_ledger_reconcile_result"][0]().handle(
        ModelReconcileRenderRequest(decide=request)
    )
    assert (result.exit_code, result.status) == (3, "blocked")
    assert result.stderr.startswith("ledger_reconcile: NOT RUN — time bounds")
    assert result.stdout == ""


def test_a_failed_append_is_exit_two_and_named_on_its_finding() -> None:
    request = decide_request(params=ModelReconcileParams(apply=True), facts=FACTS)
    result = bindings()["render_ledger_reconcile_result"][0]().handle(
        ModelReconcileRenderRequest(
            decide=request,
            outcomes=(
                ModelAppendOutcome(index=0, error="ledger_lock exit 5: busy"),
                ModelAppendOutcome(index=1),
            ),
        )
    )
    assert (result.exit_code, result.status) == (2, "blocked")
    assert "[append-failed: ledger_lock exit 5: busy]" in result.stdout
    assert "[attention-appended]" in result.stdout
    assert (result.auto_closed, result.needs_attention) == (0, 1)


def test_a_pass_over_the_cap_is_refused_whole_with_exit_four() -> None:
    request = decide_request(
        params=ModelReconcileParams(apply=True, max_appends=1), facts=FACTS
    )
    decision = bindings()["decide_ledger_reconcile"][0]().handle(request)
    assert decision.status == "decided"
    assert decision.refused_by_cap
    assert decision.planned == ()
    result = bindings()["render_ledger_reconcile_result"][0]().handle(
        ModelReconcileRenderRequest(decide=request)
    )
    assert result.exit_code == 4
    assert (
        "REFUSED — this pass would append 2 rows, more than --max-appends 1"
        in result.stderr
    )
    assert result.stdout.count("[refused-by-cap]") == 2


def test_a_lookup_failure_is_weighed_as_unresolved_never_as_landed() -> None:
    failed = ModelPrFact(repo="omnimarket", number=11, state="LOOKUP_FAILED")
    decision = _decide(
        rows=ROWS[:1],
        facts=ModelReconcileFacts(prs=(failed,)),
        params=ModelReconcileParams(apply=True),
    )
    assert decision.status == "decided"
    assert decision.planned == ()


@pytest.mark.parametrize("field", ["stale_hours", "unknown_field"])
def test_requests_are_frozen_and_refuse_unknown_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        ModelReconcileParams.model_validate({field: "not-a-number", "extra": 1})
    params = ModelReconcileParams()
    with pytest.raises(ValidationError):
        params.__setattr__("apply", True)

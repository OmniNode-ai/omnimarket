# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review gate: a new-work PR lands only on a CLEAN review verdict for its exact head (OMN-19998).

The landing decision asks the review gate before it merges a new-work head. Only a verdict on the exact
head counts: a CLEAN one on the head merges it, a CHANGES_NEEDED one holds it as ``review_changes_needed``,
verdicts only on other heads hold it as ``review_superseded``, and no verdict at all holds it as
``review_missing``. A PR with no review fact merges as before unless the policy expects the fact, which
holds it as ``review_unread``; a PR that is not new work is not the gate's to hold. The lab check is read
first: a check that is not a pass holds with its own reason before the gate is asked. The newest verdict
on the head wins, so a CHANGES_NEEDED then a later CLEAN merges and the reverse holds.

Three injected breaks show the assertions can fail for real: a judge that counts a CLEAN verdict on any
head, a judge that reads a CHANGES_NEEDED verdict as a pass, and a judge that reads no verdict as nothing
to hold. One more test proves the review fact is the decision's input: an unknown key in the dict is
refused, not silently dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_landing_review import (
    EnumReviewGateVerdict,
    EnumReviewVerdict,
    ModelReviewGateReading,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers import (
    handler_pr_landing_decision as decision_module,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingLandSkipReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 13, 0, 0, tzinfo=UTC)
PR = "OmniNode-ai/omnimarket#9001"
HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
CHECK = "lab-tests / run"
CURRENT = "dispatch-41:order-7:overlay-a1"
SUPERSEDED = "dispatch-40:order-7:overlay-a1"


def _run(attempt: str, conclusion: str = "success", head: str = HEAD) -> dict[str, Any]:
    return {
        "name": CHECK,
        "head_sha": head,
        "attempt_id": attempt,
        "conclusion": conclusion,
    }


def _lab_check(*runs: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "check_name": CHECK,
        "current_attempt_id": CURRENT,
        "runs": list(runs),
        **extra,
    }


def _row(head: str, verdict: str, at: datetime) -> dict[str, Any]:
    """One recorded review verdict: the head it judged, what it said, when."""
    return {"head_sha": head, "verdict": verdict, "at": at}


def _review(*rows: dict[str, Any], new_work: bool = True) -> dict[str, Any]:
    return {"new_work": new_work, "verdicts": list(rows)}


def _facts(
    review: dict[str, Any] | None, policy: dict[str, Any] | None = None, **pr: Any
) -> ModelLandingFacts:
    row: dict[str, Any] = {
        "pr": PR,
        "head_sha": HEAD,
        "state": "open",
        "ci": "green",
        "merge_state": "clean",
        "created_at": "2026-11-07T11:00:00Z",
        **pr,
    }
    if review is not None:
        row["review"] = review
    facts: dict[str, Any] = {
        "tick": 5,
        "observed_at": NOW.isoformat(),
        "prs": [row],
        "state": {"last_tick": 4, "records": []},
    }
    if policy is not None:
        facts["policy"] = policy
    return ModelLandingFacts.model_validate(facts)


def _merges(facts: ModelLandingFacts) -> list[str]:
    return [
        a.head_sha or ""
        for a in decide_landing(facts).actions
        if a.kind is EnumLandingActionKind.MERGE and a.subject == PR
    ]


def _skip(facts: ModelLandingFacts) -> EnumLandingLandSkipReason | None:
    rows = [s for s in decide_landing(facts).land_skips if s.pr == PR]
    return rows[0].reason if rows else None


def _judge(
    facts: ModelLandingFacts, expect_skip: EnumLandingLandSkipReason | None
) -> None:
    """The exact expectation of one decision: the head merges, or it is held by that name."""
    assert _merges(facts) == ([] if expect_skip is not None else [HEAD])
    assert _skip(facts) is expect_skip


# ---------------------------------------------------------------------------------------------- golden


def test_a_new_work_head_with_a_clean_verdict_on_it_merges() -> None:
    facts = _facts(_review(_row(HEAD, "clean", NOW - timedelta(minutes=10))))
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None


def test_a_new_work_head_with_no_verdicts_is_held_review_missing() -> None:
    facts = _facts(_review())
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.REVIEW_MISSING


def test_a_new_work_head_with_changes_needed_on_it_is_held_review_changes_needed() -> (
    None
):
    facts = _facts(_review(_row(HEAD, "changes_needed", NOW - timedelta(minutes=10))))
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.REVIEW_CHANGES_NEEDED


def test_a_new_work_head_with_a_verdict_only_on_another_head_is_held_review_superseded() -> (
    None
):
    facts = _facts(_review(_row(OLD_HEAD, "clean", NOW - timedelta(minutes=10))))
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.REVIEW_SUPERSEDED


def test_changes_needed_then_a_later_clean_on_the_head_merges() -> None:
    facts = _facts(
        _review(
            _row(HEAD, "changes_needed", NOW - timedelta(minutes=30)),
            _row(HEAD, "clean", NOW - timedelta(minutes=10)),
        )
    )
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None


def test_clean_then_a_later_changes_needed_on_the_head_is_held_review_changes_needed() -> (
    None
):
    facts = _facts(
        _review(
            _row(HEAD, "clean", NOW - timedelta(minutes=30)),
            _row(HEAD, "changes_needed", NOW - timedelta(minutes=10)),
        )
    )
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.REVIEW_CHANGES_NEEDED


def test_a_pr_that_is_not_new_work_merges_as_before() -> None:
    assert _merges(_facts(_review(new_work=False))) == [HEAD]


def test_a_pr_with_no_review_fact_and_the_default_policy_merges_as_before() -> None:
    assert _merges(_facts(None)) == [HEAD]


def test_a_pr_with_no_review_fact_under_review_read_expected_is_held_review_unread() -> (
    None
):
    facts = _facts(None, policy={"review_read_expected": True})
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.REVIEW_UNREAD


def test_a_lab_check_that_is_not_a_pass_holds_with_the_lab_check_reason_first() -> None:
    """The lab check is read before the gate: a CLEAN review verdict cannot outrank a failed check."""
    facts = _facts(
        _review(_row(HEAD, "clean", NOW - timedelta(minutes=10))),
        lab_check=_lab_check(_run(CURRENT, "failure"), _run(SUPERSEDED)),
    )
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.LAB_CHECK_FAILED


def test_a_red_or_not_clean_new_work_head_gets_no_merge_from_the_gate() -> None:
    """The gate only ever holds; it adds no merge to a PR the merge path never reached."""
    red = _facts(
        _review(_row(HEAD, "clean", NOW - timedelta(minutes=10))),
        ci="red",
        red_class="product",
    )
    assert _merges(red) == []
    conflicting = _facts(
        _review(_row(HEAD, "clean", NOW - timedelta(minutes=10))),
        merge_state="conflicting",
    )
    assert _merges(conflicting) == []


def test_the_same_facts_decide_the_same_twice() -> None:
    clean = _facts(_review(_row(HEAD, "clean", NOW - timedelta(minutes=10))))
    held = _facts(_review(_row(HEAD, "changes_needed", NOW - timedelta(minutes=10))))
    assert decide_landing(clean) == decide_landing(clean)
    assert decide_landing(held) == decide_landing(held)


# -------------------------------------------------------------------------------------------- controls


def test_break_counts_any_head_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = _facts(_review(_row(OLD_HEAD, "clean", NOW - timedelta(minutes=10))))
    _judge(facts, EnumLandingLandSkipReason.REVIEW_SUPERSEDED)  # the real gate holds it

    real = decision_module.read_review_gate

    def counts_any_head(p: Any, policy: Any) -> Any:
        assert p.review is not None
        if any(r.verdict is EnumReviewVerdict.CLEAN for r in p.review.verdicts):
            return ModelReviewGateReading(
                verdict=EnumReviewGateVerdict.PASS,
                detail="a clean verdict on any head counts",
            )
        return real(p, policy)

    monkeypatch.setattr(decision_module, "read_review_gate", counts_any_head)
    with pytest.raises(AssertionError):
        _judge(facts, EnumLandingLandSkipReason.REVIEW_SUPERSEDED)


def test_break_ignores_changes_needed_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _facts(_review(_row(HEAD, "changes_needed", NOW - timedelta(minutes=10))))
    # the real gate holds it
    _judge(facts, EnumLandingLandSkipReason.REVIEW_CHANGES_NEEDED)

    real = decision_module.read_review_gate

    def ignores_changes_needed(p: Any, policy: Any) -> Any:
        assert p.review is not None
        if any(
            r.head_sha == p.head_sha and r.verdict is EnumReviewVerdict.CHANGES_NEEDED
            for r in p.review.verdicts
        ):
            return ModelReviewGateReading(
                verdict=EnumReviewGateVerdict.PASS,
                detail="a changes_needed verdict reads as a pass",
            )
        return real(p, policy)

    monkeypatch.setattr(decision_module, "read_review_gate", ignores_changes_needed)
    with pytest.raises(AssertionError):
        _judge(facts, EnumLandingLandSkipReason.REVIEW_CHANGES_NEEDED)


def test_break_missing_reads_pass_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    facts = _facts(_review())
    _judge(facts, EnumLandingLandSkipReason.REVIEW_MISSING)  # the real gate holds it

    real = decision_module.read_review_gate

    def missing_reads_pass(p: Any, policy: Any) -> Any:
        assert p.review is not None
        if not p.review.verdicts:
            return None  # no verdict reads as nothing to hold: the head merges
        return real(p, policy)

    monkeypatch.setattr(decision_module, "read_review_gate", missing_reads_pass)
    with pytest.raises(AssertionError):
        _judge(facts, EnumLandingLandSkipReason.REVIEW_MISSING)


# ----------------------------------------------------------------------------------------------- fact


def test_the_review_fact_refuses_an_unknown_key() -> None:
    """The review dict is the decision's input: an unknown key is refused, never dropped."""
    with pytest.raises(ValidationError):
        _facts({"new_work": True, "verdicts": [], "verdict_count": 0})

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RED-first coverage for bounded refreshes of stale cancelled, blocked heads.

Facts and state enter through plain documents so the new fields and merge state
must be accepted by validation before the decision rules can satisfy the tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingBriefClass,
    EnumLandingDegradedReason,
    EnumLandingEngine,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingControllerState,
    ModelLandingPrRecord,
)

KEY = "OmniNode-ai/omnibase_core#1869"
HEAD_OLD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
HEAD_NEW = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
PARENT = "OmniNode-ai/omnibase_core#1800"
CREATED_AT = "2026-10-03T00:00:00Z"


def _facts(
    tick: int,
    prs: Sequence[dict[str, Any]],
    records: Sequence[dict[str, Any]] = (),
    observed_at: str = "2026-10-04T05:00:00Z",
) -> ModelLandingFacts:
    return ModelLandingFacts.model_validate(
        {
            "tick": tick,
            "observed_at": observed_at,
            "prs": list(prs),
            "state": {"last_tick": tick - 1, "records": list(records)},
        }
    )


def _stale(**fields: Any) -> dict[str, Any]:
    return {
        "pr": KEY,
        "head_sha": HEAD_OLD,
        "state": "open",
        "ci": "red",
        "red_class": "product",
        "red_checks": [],
        "merge_state": "blocked",
        "cancelled_checks": ["occ-preflight / eligibility"],
        "suspensions": [],
        "collaborator": False,
        "created_at": CREATED_AT,
        **fields,
    }


def _real_red(**fields: Any) -> dict[str, Any]:
    return {
        "pr": KEY,
        "head_sha": HEAD_NEW,
        "state": "open",
        "ci": "red",
        "red_class": "product",
        "red_checks": ["CI Summary", "Quality Gate", "Tests Gate"],
        "merge_state": "unknown",
        "suspensions": [],
        "collaborator": False,
        "created_at": CREATED_AT,
        **fields,
    }


def _clean(pr: str, head_sha: str) -> dict[str, Any]:
    return {
        "pr": pr,
        "head_sha": head_sha,
        "state": "open",
        "ci": "green",
        "merge_state": "clean",
        "suspensions": [],
        "collaborator": False,
        "created_at": CREATED_AT,
    }


def _actions(decision: ModelLandingDecision) -> list[tuple[str, str]]:
    return [(a.kind.value, a.subject) for a in decision.actions]


def _record(decision: ModelLandingDecision, pr: str = KEY) -> ModelLandingPrRecord:
    return next(r for r in decision.next_state.records if r.pr == pr)


def _assert_update(decision: ModelLandingDecision, head_sha: str = HEAD_OLD) -> None:
    assert _actions(decision) == [(EnumLandingActionKind.UPDATE_BRANCH.value, KEY)]
    assert decision.actions[0].head_sha == head_sha


def _assert_worker(decision: ModelLandingDecision) -> None:
    assert _actions(decision) == [(EnumLandingActionKind.DISPATCH_WORKER.value, KEY)]
    brief = decision.actions[0].brief
    assert brief is not None
    assert brief.brief_class is EnumLandingBriefClass.REAL_RED


@pytest.mark.unit
def test_stale_cancelled_blocked_emits_one_update_branch() -> None:
    decision = decide_landing(_facts(1, [_stale()]))
    _assert_update(decision)
    assert decision.degraded == ()
    assert decision.violations == ()
    record = _record(decision)
    assert record.update_heads == (HEAD_OLD,)
    assert record.stale_refreshes == 1
    assert record.ladder_index == 0


@pytest.mark.unit
def test_stale_cancelled_blocked_next_tick_same_head_emits_nothing() -> None:
    first = decide_landing(_facts(1, [_stale()]))
    _assert_update(first)
    facts = ModelLandingFacts.model_validate(
        {
            **_facts(2, [_stale()]).model_dump(mode="json"),
            "state": first.next_state.model_dump(mode="json"),
        }
    )
    decision = decide_landing(facts)
    assert decision.actions == ()
    assert decision.degraded == ()
    assert _record(decision) == _record(first)
    assert _record(decision).update_heads == (HEAD_OLD,)
    assert _record(decision).stale_refreshes == 1


@pytest.mark.unit
def test_stale_cancelled_blocked_with_stale_external_blocker_record() -> None:
    record = {
        "pr": KEY,
        "outcome": "external_blocker",
        "blocker_fingerprint": "stale-fingerprint",
        "ladder_index": 0,
    }
    decision = decide_landing(_facts(1, [_stale()], records=[record]))
    _assert_update(decision)


@pytest.mark.unit
def test_stale_cancelled_blocked_green_ci_is_the_same_class() -> None:
    decision = decide_landing(_facts(1, [_stale(ci="green", red_class=None)]))
    _assert_update(decision)


@pytest.mark.unit
def test_stale_cancelled_blocked_cancelled_producer_class_refreshes_not_reruns() -> (
    None
):
    decision = decide_landing(_facts(1, [_stale(red_class="cancelled_producer")]))
    _assert_update(decision)


@pytest.mark.unit
def test_stale_cancelled_blocked_pending_ci_is_left_alone() -> None:
    decision = decide_landing(_facts(1, [_stale(ci="pending")]))
    assert decision.actions == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"suspensions": ["hold"]}, id="suspended"),
        pytest.param({"collaborator": True}, id="collaborator"),
        pytest.param({"parents": [PARENT], "open_parents": [PARENT]}, id="open-parent"),
    ],
)
def test_stale_cancelled_blocked_ignores_suspended_collaborator_and_parented_prs(
    fields: dict[str, Any],
) -> None:
    prs = [_stale(**fields)]
    if "parents" in fields:
        prs.append(_clean(PARENT, "d" * 40))
    decision = decide_landing(_facts(1, prs))
    assert not any(a.subject == KEY for a in decision.actions)


@pytest.mark.unit
def test_stale_cancelled_blocked_under_a_lease_is_left_alone() -> None:
    lease = {
        "pr": KEY,
        "lease_id": 1,
        "brief_class": "real_red",
        "engine": "claude_sonnet",
        "dispatched_at": "2026-10-04T04:50:00Z",
        "deadline_at": "2026-10-04T05:50:00Z",
        "dispatch_head": HEAD_OLD,
        "seen_heads": [HEAD_OLD],
        "last_seen_head": HEAD_OLD,
    }
    base = _facts(1, [_stale()]).model_dump(mode="json")
    facts = ModelLandingFacts.model_validate(
        {
            **base,
            "state": {**base["state"], "next_lease_id": 2, "leases": [lease]},
            "probes": [{"lease_id": 1, "group_alive": True, "tagged_alive": True}],
        }
    )
    decision = decide_landing(facts)
    assert decision.actions == ()
    assert len(decision.next_state.leases) == 1


@pytest.mark.unit
def test_stale_cancelled_blocked_loop_bound_two_refreshes_then_degraded() -> None:
    decision = decide_landing(_facts(1, [_stale(head_sha="a" * 40)]))
    _assert_update(decision, "a" * 40)
    assert _record(decision).stale_refreshes == 1
    assert _record(decision).ladder_index == 0
    assert _record(decision).parked_head is None
    assert decision.degraded == ()
    for tick, head in ((2, "b" * 40), (3, "c" * 40), (4, "c" * 40)):
        facts = ModelLandingFacts.model_validate(
            {
                **_facts(tick, [_stale(head_sha=head)]).model_dump(mode="json"),
                "state": decision.next_state.model_dump(mode="json"),
            }
        )
        decision = decide_landing(facts)
        record = _record(decision)
        assert record.stale_refreshes == 2
        assert record.update_heads == ("a" * 40, "b" * 40)
        assert record.ladder_index == 0
        assert record.parked_head is None
        if tick == 2:
            _assert_update(decision, head)
            assert decision.degraded == ()
        else:
            assert decision.actions == ()
            assert [(d.reason, d.subject) for d in decision.degraded] == [
                (EnumLandingDegradedReason.STALE_REFRESH_EXHAUSTED, KEY)
            ]
            assert decision.degraded[0].reason.value == "stale_refresh_exhausted"


@pytest.mark.unit
def test_stale_cancelled_blocked_exhausted_with_real_red_still_gets_a_worker() -> None:
    decision = decide_landing(
        _facts(
            1,
            [_stale(red_checks=["Unit Tests"])],
            records=[{"pr": KEY, "stale_refreshes": 2}],
        )
    )
    _assert_worker(decision)
    assert decision.degraded == ()


@pytest.mark.unit
@pytest.mark.parametrize("ci", ["green", "red"])
def test_blocked_without_cancelled_copies_gets_no_update_branch(ci: str) -> None:
    pr = _stale(
        ci=ci,
        red_class="product" if ci == "red" else None,
        red_checks=["Unit Tests"] if ci == "red" else [],
        cancelled_checks=[],
    )
    decision = decide_landing(_facts(1, [pr]))
    if ci == "green":
        assert decision.actions == ()
    else:
        _assert_worker(decision)
        unknown = decide_landing(_facts(1, [{**pr, "merge_state": "unknown"}]))
        assert decision.actions == unknown.actions


@pytest.mark.unit
def test_stale_cancelled_real_red_one_failed_copy_gets_the_real_red_worker() -> None:
    decision = decide_landing(_facts(1, [_stale(red_checks=["Unit Tests"])]))
    _assert_worker(decision)


@pytest.mark.unit
def test_stale_cancelled_real_red_failed_attempt_on_same_head_escalates() -> None:
    record = {
        "pr": KEY,
        "awaiting_head": HEAD_OLD,
        "attempt_red_checks": ["Unit Tests"],
        "ladder_index": 0,
    }
    decision = decide_landing(
        _facts(1, [_stale(red_checks=["Unit Tests"])], records=[record])
    )
    _assert_worker(decision)
    assert _record(decision).ladder_index == 1
    brief = decision.actions[0].brief
    assert brief is not None
    assert brief.engine is EnumLandingEngine.CLAUDE_OPUS


@pytest.mark.unit
def test_stale_cancelled_real_red_f7402aa_shape_still_gets_a_worker() -> None:
    """The 05:20Z controller record also carried suspensions [hold].

    Those suppress the worker, so this negative control omits the hold.
    """
    decision = decide_landing(_facts(1, [_real_red()]))
    _assert_worker(decision)


@pytest.mark.unit
def test_stale_cancelled_real_red_blocked_with_failed_copy_is_not_refreshed() -> None:
    pr = _real_red(
        merge_state="blocked", cancelled_checks=["occ-preflight / eligibility"]
    )
    decision = decide_landing(_facts(1, [pr]))
    _assert_worker(decision)


@pytest.mark.unit
@pytest.mark.parametrize(
    "pr",
    [
        pytest.param(_real_red(), id="1869-real-red"),
        pytest.param(
            _clean("OmniNode-ai/omnibase_core#1877", "6b76bc85".ljust(40, "0")),
            id="1877-clean-green",
        ),
    ],
)
def test_legacy_facts_decide_as_before(pr: dict[str, Any]) -> None:
    assert "cancelled_checks" not in pr
    decision = decide_landing(_facts(1, [pr], records=[{"pr": pr["pr"]}]))
    if pr["pr"] == KEY:
        _assert_worker(decision)
    else:
        assert _actions(decision) == [(EnumLandingActionKind.MERGE.value, pr["pr"])]
        assert decision.actions[0].head_sha == pr["head_sha"]
    assert _record(decision, pr["pr"]).update_heads == ()


@pytest.mark.unit
def test_legacy_facts_leave_the_new_record_field_at_its_default() -> None:
    decision = decide_landing(_facts(1, [_real_red()], records=[{"pr": KEY}]))
    assert _record(decision).stale_refreshes == 0


@pytest.mark.unit
def test_legacy_facts_unknown_merge_state_with_cancelled_red_still_dispatches() -> None:
    pr = _real_red(head_sha=HEAD_OLD, red_checks=[])
    decision = decide_landing(_facts(1, [pr]))
    _assert_worker(decision)


@pytest.mark.unit
def test_legacy_state_record_without_new_field_loads() -> None:
    state = ModelLandingControllerState.model_validate(
        {"last_tick": 0, "records": [{"pr": KEY, "ladder_index": 0}]}
    )
    assert state.records[0].stale_refreshes == 0

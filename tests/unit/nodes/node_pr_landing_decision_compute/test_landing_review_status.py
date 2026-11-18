# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing decision holds a new-work PR whose head lacks a success fable-review-gate commit status.

Beside the ledger verdict gate, a new-work head merges only with a success ``fable-review-gate`` status on the exact
head. A status on an older head does not count. Each non-pass reading holds the head with its own named reason:
``review_status_missing``, ``review_status_failing``, ``review_status_other_head`` and, when the policy expects the
statuses to be read (the default) and none were, ``review_status_unread``. A PR that is not new work, or whose
statuses were not supplied while the policy does not expect them, is decided as before. Every hold has its control: the same facts with
a success status on the head merge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

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
PR = "OmniNode-ai/omnimarket#9002"
HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
GATE = "fable-review-gate"
CLEAN = {"head_sha": HEAD, "verdict": "clean", "at": NOW}


def _status(
    head: str, state: str, *, minute: int = 0, context: str = GATE
) -> dict[str, Any]:
    return {
        "head_sha": head,
        "context": context,
        "state": state,
        "at": NOW.replace(minute=minute),
    }


def _facts(
    statuses: list[dict[str, Any]] | None,
    *,
    new_work: bool = True,
    policy: dict[str, Any] | None = None,
) -> ModelLandingFacts:
    review: dict[str, Any] = {"new_work": new_work, "verdicts": [CLEAN]}
    if statuses is not None:
        review["statuses"] = statuses
    facts: dict[str, Any] = {
        "tick": 5,
        "observed_at": NOW.isoformat(),
        "prs": [
            {
                "pr": PR,
                "head_sha": HEAD,
                "state": "open",
                "ci": "green",
                "merge_state": "clean",
                "created_at": "2026-11-07T11:00:00Z",
                "review": review,
            }
        ],
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


def test_landing_review_status_merges_a_new_work_head_with_a_success_status() -> None:
    facts = _facts([_status(HEAD, "success")])
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None


def test_landing_review_status_holds_a_head_with_no_status_as_missing() -> None:
    facts = _facts([])
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.REVIEW_STATUS_MISSING


@pytest.mark.parametrize("state", ["failure", "pending", "error"])
def test_landing_review_status_holds_a_status_that_is_not_success_as_failing(
    state: str,
) -> None:
    facts = _facts([_status(HEAD, state)])
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.REVIEW_STATUS_FAILING


def test_landing_review_status_on_an_older_head_does_not_count() -> None:
    facts = _facts([_status(OLD_HEAD, "success")])
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.REVIEW_STATUS_OTHER_HEAD


def test_landing_review_status_newest_on_the_head_decides() -> None:
    red_last = _facts(
        [_status(HEAD, "success", minute=1), _status(HEAD, "failure", minute=2)]
    )
    assert _skip(red_last) is EnumLandingLandSkipReason.REVIEW_STATUS_FAILING
    green_last = _facts(
        [_status(HEAD, "failure", minute=1), _status(HEAD, "success", minute=2)]
    )
    assert _merges(green_last) == [HEAD]


def test_landing_review_status_ignores_a_status_of_another_context() -> None:
    facts = _facts([_status(HEAD, "success", context="ci/other")])
    assert _skip(facts) is EnumLandingLandSkipReason.REVIEW_STATUS_MISSING


def test_landing_review_status_unread_statuses_hold_only_when_the_policy_expects_them() -> (
    None
):
    expected = _facts(None)
    assert _merges(expected) == []
    assert _skip(expected) is EnumLandingLandSkipReason.REVIEW_STATUS_UNREAD
    not_expected = _facts(None, policy={"review_status_read_expected": False})
    assert _merges(not_expected) == [HEAD]


def test_landing_review_status_does_not_gate_a_pr_that_is_not_new_work() -> None:
    assert _merges(_facts([], new_work=False)) == [HEAD]


def test_landing_review_status_the_ledger_verdict_gate_is_still_asked_first() -> None:
    facts = _facts([_status(HEAD, "success")])
    review = facts.prs[0].review
    assert review is not None
    no_verdict = ModelLandingFacts.model_validate(
        {
            **facts.model_dump(mode="json"),
            "prs": [
                {
                    **facts.prs[0].model_dump(mode="json"),
                    "review": {
                        "new_work": True,
                        "verdicts": [],
                        "statuses": [_status(HEAD, "success")],
                    },
                }
            ],
        }
    )
    assert _skip(no_verdict) is EnumLandingLandSkipReason.REVIEW_MISSING

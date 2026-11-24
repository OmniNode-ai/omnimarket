# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing decision holds a core or spi pull request whose head broke a downstream repository (OMN-26513).

A PR into a repository named in ``policy.downstream_gate_repos`` merges only on a pass of the downstream check on
its exact head. A break, an unread check and a check of an older head each hold the head with a named reason, and
every hold has its control: the same facts with a pass on the head merge. A PR into another repository, and a
core PR with no downstream fact while the policy does not expect one, are decided as before.
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

NOW = datetime(2026, 11, 23, 20, 0, 0, tzinfo=UTC)
HEAD = "12e5044971c3cf2dd1160b14c35bd0d63c920578"
OLD_HEAD = "9d078605085efa11174d663730f3b8311e42efce"
CORE_PR = "OmniNode-ai/omnibase_core#2361"
OTHER_PR = "OmniNode-ai/omnimarket#9002"


def _fact(head: str, verdict: str, reasons: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "repo": "omnibase_core",
        "head_sha": head,
        "verdict": verdict,
        "reasons": list(reasons),
    }


def _facts(
    pr: str, downstream: dict[str, Any] | None, *, policy: dict[str, Any] | None = None
) -> ModelLandingFacts:
    row: dict[str, Any] = {
        "pr": pr,
        "head_sha": HEAD,
        "state": "open",
        "ci": "green",
        "merge_state": "clean",
        "created_at": "2026-11-23T19:00:00Z",
    }
    if downstream is not None:
        row["downstream"] = downstream
    facts: dict[str, Any] = {
        "tick": 5,
        "observed_at": NOW.isoformat(),
        "prs": [row],
        "state": {"last_tick": 4, "records": []},
    }
    if policy is not None:
        facts["policy"] = policy
    return ModelLandingFacts.model_validate(facts)


def _merges(facts: ModelLandingFacts, pr: str) -> list[str]:
    return [
        a.head_sha or ""
        for a in decide_landing(facts).actions
        if a.kind is EnumLandingActionKind.MERGE and a.subject == pr
    ]


def _skip(facts: ModelLandingFacts, pr: str) -> EnumLandingLandSkipReason | None:
    rows = [s for s in decide_landing(facts).land_skips if s.pr == pr]
    return rows[0].reason if rows else None


def test_a_core_head_with_a_passing_downstream_check_merges() -> None:
    facts = _facts(CORE_PR, _fact(HEAD, "pass"))
    assert _merges(facts, CORE_PR) == [HEAD]
    assert _skip(facts, CORE_PR) is None


def test_a_core_head_that_broke_a_downstream_repository_is_held_as_a_break() -> None:
    facts = _facts(
        CORE_PR,
        _fact(
            HEAD, "break", ("omnimarket: x cannot import y at core_ticket_guard.py:20",)
        ),
    )
    assert _merges(facts, CORE_PR) == []
    assert _skip(facts, CORE_PR) is EnumLandingLandSkipReason.DOWNSTREAM_BREAK


def test_a_check_that_gave_no_answer_holds_the_head_as_unread() -> None:
    facts = _facts(CORE_PR, _fact(HEAD, "unread", ("omnimarket: timeout:smoke",)))
    assert _merges(facts, CORE_PR) == []
    assert _skip(facts, CORE_PR) is EnumLandingLandSkipReason.DOWNSTREAM_UNREAD


def test_a_pass_on_an_older_head_does_not_count() -> None:
    facts = _facts(CORE_PR, _fact(OLD_HEAD, "pass"))
    assert _merges(facts, CORE_PR) == []
    assert _skip(facts, CORE_PR) is EnumLandingLandSkipReason.DOWNSTREAM_OTHER_HEAD


def test_a_core_pr_with_no_fact_is_decided_as_before_unless_the_policy_expects_one() -> (
    None
):
    assert _merges(_facts(CORE_PR, None), CORE_PR) == [HEAD]
    expecting = _facts(CORE_PR, None, policy={"downstream_read_expected": True})
    assert _merges(expecting, CORE_PR) == []
    assert _skip(expecting, CORE_PR) is EnumLandingLandSkipReason.DOWNSTREAM_UNREAD


def test_a_pr_into_another_repository_is_not_the_gates_to_hold() -> None:
    facts = _facts(
        OTHER_PR,
        _fact(HEAD, "break"),
        policy={"downstream_read_expected": True},
    )
    assert _merges(facts, OTHER_PR) == [HEAD]


def test_the_gated_repositories_come_from_the_policy() -> None:
    facts = _facts(
        OTHER_PR,
        _fact(HEAD, "break"),
        policy={"downstream_gate_repos": ["omnimarket"]},
    )
    assert _skip(facts, OTHER_PR) is EnumLandingLandSkipReason.DOWNSTREAM_BREAK

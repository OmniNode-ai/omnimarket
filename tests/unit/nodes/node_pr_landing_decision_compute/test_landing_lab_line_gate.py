# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing decision merges a PR only when its newest Lab line names the exact head it merges (OMN-26736).

A Lab line naming an older head, or no Lab line at all, holds the head as ``lab_line_needs_proof`` and dispatches
one proof worker for that head. A head the controller made by updating the branch is a new head: the Lab line of
the old head does not carry over. The same facts with a Lab line on the exact head merge.
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
    EnumLandingBriefClass,
    EnumLandingLandSkipReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 28, 7, 0, 0, tzinfo=UTC)
HEAD = "12e5044971c3cf2dd1160b14c35bd0d63c920578"
OLD_HEAD = "9d078605085efa11174d663730f3b8311e42efce"
PR = "OmniNode-ai/omnimarket#6999"


def _facts(
    *,
    head: str = HEAD,
    merge_state: str = "clean",
    lab_line: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    tick: int = 5,
) -> ModelLandingFacts:
    row: dict[str, Any] = {
        "pr": PR,
        "head_sha": head,
        "state": "open",
        "ci": "green",
        "merge_state": merge_state,
        "created_at": "2026-11-28T06:00:00Z",
    }
    if lab_line is not None:
        row["lab_line"] = lab_line
    facts: dict[str, Any] = {
        "tick": tick,
        "observed_at": NOW.isoformat(),
        "prs": [row],
        "state": state or {"last_tick": 4, "records": []},
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


def _proof_dispatches(facts: ModelLandingFacts) -> list[str]:
    return [
        a.head_sha or ""
        for a in decide_landing(facts).actions
        if a.kind is EnumLandingActionKind.DISPATCH_WORKER
        and a.brief is not None
        and a.brief.brief_class is EnumLandingBriefClass.PROOF
    ]


def _skip(facts: ModelLandingFacts) -> EnumLandingLandSkipReason | None:
    rows = [s for s in decide_landing(facts).land_skips if s.pr == PR]
    return rows[0].reason if rows else None


def test_ac1_a_lab_line_naming_an_older_head_is_not_merged_and_needs_proof() -> None:
    facts = _facts(lab_line={"named_head": OLD_HEAD})
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.LAB_LINE_NEEDS_PROOF
    assert _proof_dispatches(facts) == [HEAD]


def test_ac3_a_lab_line_naming_the_exact_head_still_merges() -> None:
    facts = _facts(lab_line={"named_head": HEAD})
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None
    assert _proof_dispatches(facts) == []


def test_a_pr_with_no_lab_line_fact_is_decided_as_before_unless_the_policy_expects_one() -> (
    None
):
    assert _merges(_facts()) == [HEAD]
    expecting = _facts(policy={"lab_line_read_expected": True})
    assert _merges(expecting) == []
    assert _skip(expecting) is EnumLandingLandSkipReason.LAB_LINE_NEEDS_PROOF
    assert _proof_dispatches(expecting) == [HEAD]


def test_a_pr_with_no_lab_line_in_its_body_or_comments_needs_proof() -> None:
    facts = _facts(lab_line={"named_head": None})
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.LAB_LINE_NEEDS_PROOF


def test_ac2_a_head_made_by_update_branch_goes_back_to_needs_proof() -> None:
    # Tick 5: behind, proven at the old head -> the controller updates the branch.
    behind = _facts(
        head=OLD_HEAD,
        merge_state="behind",
        lab_line={"named_head": OLD_HEAD},
    )
    first = decide_landing(behind)
    assert [a.kind for a in first.actions] == [EnumLandingActionKind.UPDATE_BRANCH]
    # Tick 6: the update made a new head; the Lab line still names the old head.
    after = _facts(head=HEAD, lab_line={"named_head": OLD_HEAD})
    assert _merges(after) == []
    assert _skip(after) is EnumLandingLandSkipReason.LAB_LINE_NEEDS_PROOF
    assert _proof_dispatches(after) == [HEAD]
    # Control: a Lab line at the new head merges it.
    proven = _facts(head=HEAD, lab_line={"named_head": HEAD})
    assert _merges(proven) == [HEAD]


def test_a_proof_worker_is_dispatched_once_per_head() -> None:
    facts = _facts(lab_line={"named_head": OLD_HEAD})
    first = decide_landing(facts)
    assert _proof_dispatches(facts) == [HEAD]
    again = _facts(
        lab_line={"named_head": OLD_HEAD},
        state=first.next_state.model_dump(mode="json"),
        tick=6,
    )
    assert _proof_dispatches(again) == []
    assert _skip(again) is EnumLandingLandSkipReason.LEASED


def test_a_proof_worker_is_not_redispatched_after_its_lease_ended() -> None:
    first = decide_landing(_facts(lab_line={"named_head": OLD_HEAD}))
    state = first.next_state.model_dump(mode="json")
    state["leases"] = []
    again = _facts(lab_line={"named_head": OLD_HEAD}, state=state, tick=6)
    assert _proof_dispatches(again) == []
    assert _merges(again) == []

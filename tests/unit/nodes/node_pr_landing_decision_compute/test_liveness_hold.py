# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The liveness hold on the landing decision (OMN-23529).

The landing snapshot step runs the liveness register's completeness reader against the PR's head and the
decision reads one liveness fact per PR. A made read that names an undeclared installer source holds the
head with the file named; an unreadable read, a read of another head and an absent read where the policy
expects one hold nothing, and the decision lists each as a read not made. A made read that found no
undeclared source, and a PR with no liveness fact under the default policy, decide exactly as before.

Two injected breaks show the assertions can fail for real: a judge that names no undeclared source, and
a judge that holds an unreadable read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

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
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingLandSkip,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPrFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_liveness import (
    EnumLivenessRead,
    ModelLandingLivenessFacts,
    ModelLandingLivenessUnread,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 7, 13, 0, 0, tzinfo=UTC)
PR = "OmniNode-ai/omnimarket#9001"
HEAD = "7265930bafc673e66ced5bb67b9464bc460f1d77"
OLD_HEAD = "f7402aa63d1b74b082b174badbe07ac91dcddf36"
TIMER = "deploy/systemd/onex-new.timer"


def _liveness(
    head_sha: str = HEAD,
    read: EnumLivenessRead = EnumLivenessRead.READ,
    sources: tuple[str, ...] = (),
    detail: str = "",
) -> dict[str, Any]:
    return {
        "head_sha": head_sha,
        "read": read,
        "undeclared_sources": list(sources),
        "detail": detail,
    }


def _facts(policy: dict[str, Any] | None = None, **pr: Any) -> ModelLandingFacts:
    row: dict[str, Any] = {
        "pr": PR,
        "head_sha": HEAD,
        "state": "open",
        "ci": "green",
        "merge_state": "clean",
        "created_at": "2026-11-07T11:00:00Z",
        **pr,
    }
    body: dict[str, Any] = {
        "tick": 5,
        "observed_at": NOW.isoformat(),
        "prs": [row],
        "state": {"last_tick": 4, "records": []},
    }
    if policy is not None:
        body["policy"] = policy
    return ModelLandingFacts.model_validate(body)


def _merges(facts: ModelLandingFacts) -> list[str]:
    return [
        a.head_sha or ""
        for a in decide_landing(facts).actions
        if a.kind is EnumLandingActionKind.MERGE and a.subject == PR
    ]


def _skip_row(facts: ModelLandingFacts) -> ModelLandingLandSkip | None:
    return next((s for s in decide_landing(facts).land_skips if s.pr == PR), None)


def _skip(facts: ModelLandingFacts) -> EnumLandingLandSkipReason | None:
    row = _skip_row(facts)
    return row.reason if row is not None else None


def _unread(facts: ModelLandingFacts) -> tuple[ModelLandingLivenessUnread, ...]:
    return tuple(r for r in decide_landing(facts).liveness_unread if r.pr == PR)


# ---------------------------------------------------------------------------------------------- golden


def test_golden_a_read_with_no_undeclared_source_merges() -> None:
    facts = _facts(liveness=_liveness())
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None
    assert _unread(facts) == ()


def test_golden_a_pr_with_no_liveness_fact_decides_as_before() -> None:
    """The fact is optional: a snapshot without it merges a green, clean head exactly as the node always did."""
    facts = _facts()
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None
    assert _unread(facts) == ()


# ---------------------------------------------------------------------------------------------- error


def test_error_an_undeclared_installer_holds_the_head_and_names_the_file() -> None:
    facts = _facts(liveness=_liveness(sources=(TIMER,)))
    assert _merges(facts) == []
    row = _skip_row(facts)
    assert row is not None
    assert row.reason is EnumLandingLandSkipReason.LIVENESS_UNDECLARED
    assert row.files == (TIMER,)


def test_error_an_unreadable_read_holds_nothing_and_says_the_read_was_not_made() -> (
    None
):
    facts = _facts(
        liveness=_liveness(read=EnumLivenessRead.UNREADABLE, detail="reader timed out")
    )
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None
    rows = _unread(facts)
    assert len(rows) == 1
    assert rows[0].pr == PR
    assert rows[0].head_sha == HEAD
    assert rows[0].detail == "reader timed out"


def test_error_an_absent_read_where_expected_is_listed_as_not_made() -> None:
    facts = _facts(policy={"liveness_read_expected": True})
    assert _merges(facts) == [HEAD]
    rows = _unread(facts)
    assert len(rows) == 1
    assert rows[0].pr == PR
    assert rows[0].head_sha == HEAD
    assert rows[0].detail == "no liveness read"


def test_error_a_read_of_another_head_holds_nothing() -> None:
    facts = _facts(liveness=_liveness(head_sha=OLD_HEAD, sources=(TIMER,)))
    assert _merges(facts) == [HEAD]
    rows = _unread(facts)
    assert len(rows) == 1
    assert rows[0].pr == PR
    assert rows[0].detail == f"liveness read of another head {OLD_HEAD}"


def test_error_an_unreadable_fact_that_names_a_file_is_refused() -> None:
    with pytest.raises(
        ValidationError, match="an unreadable liveness read names no undeclared source"
    ):
        ModelLandingLivenessFacts(
            head_sha=HEAD,
            read=EnumLivenessRead.UNREADABLE,
            undeclared_sources=("x",),
            detail="d",
        )


# --------------------------------------------------------------------------------------------- controls


def _names_nothing(p: ModelLandingPrFacts) -> tuple[str, ...]:
    assert p is not None
    return ()


def _names_a_file(p: ModelLandingPrFacts) -> tuple[str, ...]:
    assert p is not None
    return ("x",)


def test_control_break_removing_the_hold_branch_merges_the_undeclared_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _facts(liveness=_liveness(sources=(TIMER,)))
    assert _merges(facts) == []  # the real judge holds it

    monkeypatch.setattr(decision_module, "_liveness_undeclared", _names_nothing)
    assert _merges(facts) == [HEAD], "the break must turn the error assertion red"


def test_control_break_an_unreadable_read_that_holds_is_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = _facts(
        liveness=_liveness(read=EnumLivenessRead.UNREADABLE, detail="reader timed out")
    )
    assert _merges(facts) == [HEAD]  # the real judge holds nothing

    monkeypatch.setattr(decision_module, "_liveness_undeclared", _names_a_file)
    assert _merges(facts) == [], "the break must turn the error assertion red"

    monkeypatch.setattr(decision_module, "_liveness_undeclared", _names_a_file)
    assert _merges(facts) == [], "the break must turn the unreadable assertion red"

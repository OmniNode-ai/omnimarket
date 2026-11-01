# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_handoff_decision_compute: a scope that is absent, unreadable or invalid refuses, never decides ready.

The landing scope is a fact the caller reads from its ``landing_scope`` overlay. A request
that does not carry a usable one is refused ``scope-unread`` before anything else is decided.
Repository names in the fixtures are invented.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from omnimarket.models.pr_handoff import (
    EnumPrHandoffErrorCode,
    EnumPrHandoffVerdict,
    ModelPrHandoffDecision,
    ModelPrHandoffDecisionRequest,
)
from omnimarket.nodes.node_pr_handoff_decision_compute.handlers.handler_pr_handoff_decision import (
    HandlerPrHandoffDecision,
)
from tests.chains.pr_handoff import _builders as b

pytestmark = pytest.mark.unit

CID = UUID("6f0c3a52-8d2b-4a51-9a50-55a1d3f2b0c4")
E = EnumPrHandoffErrorCode
REPO = "acme-widgets"


def _decide(*, observed: bool = True, **scope: Any) -> ModelPrHandoffDecision:
    return HandlerPrHandoffDecision().handle(
        ModelPrHandoffDecisionRequest(
            request=b.request(CID, repo=REPO, claim_prs=(), lab_proof=b.lab_comment()),
            observation=(
                b.observation(b.at(60), repo=REPO).observation() if observed else None
            ),
            now=b.at(60),
            **scope,
        )
    )


def _unread(decision: ModelPrHandoffDecision) -> None:
    assert decision.verdict is EnumPrHandoffVerdict.REFUSE
    assert decision.error_code is E.SCOPE_UNREAD
    assert E.SCOPE_UNREAD.value == "scope-unread"
    assert decision.rows is None
    assert "landing_scope" in decision.detail


def test_an_absent_scope_is_refused_scope_unread_and_never_ready() -> None:
    _unread(_decide())
    _unread(_decide(observed=False))
    _unread(_decide(landing_scope=None))


def test_an_unreadable_scope_is_refused_scope_unread_and_never_ready() -> None:
    problem = "overlay landing_scope is unreadable (OSError)"
    decision = _decide(landing_scope=None, landing_scope_problem=problem)
    _unread(decision)
    assert problem in decision.detail
    # Even a scope that arrived beside a recorded read problem is not trusted.
    _unread(_decide(landing_scope=(REPO,), landing_scope_problem=problem))


@pytest.mark.parametrize(
    "scope",
    [("",), ("  ",), (REPO, ""), (f" {REPO}",), (f"{REPO} ",)],
    ids=["empty", "blank", "one-empty", "leading-space", "trailing-space"],
)
def test_an_invalid_scope_is_refused_scope_unread_and_never_ready(
    scope: tuple[str, ...],
) -> None:
    _unread(_decide(landing_scope=scope))


def test_a_refusal_for_an_unread_scope_carries_no_ledger_row() -> None:
    assert _decide().refusal_row is None


def test_a_valid_scope_is_still_decided() -> None:
    assert _decide(landing_scope=(REPO,)).verdict is EnumPrHandoffVerdict.READY
    assert _decide(landing_scope=("other-repo",)).error_code is (
        E.REPO_OUT_OF_LANDING_SCOPE
    )
    # An empty scope is a valid scope that lands nothing, not an unread one.
    assert _decide(landing_scope=()).error_code is E.REPO_OUT_OF_LANDING_SCOPE

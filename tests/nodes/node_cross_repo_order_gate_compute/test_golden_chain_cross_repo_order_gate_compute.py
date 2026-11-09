# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the cross-repository order gate (OMN-23345).

The replay is the stall of 2026-11-09: manifest-repo#1531 removed the intelligence-api lane-manifest entries
and merged at 04:49Z; omnibase_infra#5810, the half that removes the containers, merged at 14:29Z.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_cross_repo_order_gate_compute import (
    HandlerCrossRepoOrderGate,
)
from omnimarket.nodes.node_cross_repo_order_gate_compute.handlers.handler_cross_repo_order_gate import (
    order_gate,
)
from omnimarket.nodes.node_cross_repo_order_gate_compute.models import (
    EnumCrossRepoOrderReason as Reason,
)
from omnimarket.nodes.node_cross_repo_order_gate_compute.models import (
    EnumCrossRepoOrderVerdict as Verdict,
)
from omnimarket.nodes.node_cross_repo_order_gate_compute.models import (
    ModelCrossRepoOrderGateRequest as Request,
)

MANIFEST = "manifest-repo#1531"  # removes the manifest entries: must follow the fold
FOLD = "omnibase_infra#5810"  # removes the containers from compose
DECLARED = f"Remove the intelligence-api entries.\n\nLands-After: {FOLD}\n"
SIBLING_DECLARES = f"The fold.\n\nLands-Before: {MANIFEST}\n"


def _request(bodies: dict[str, str], fold_state: str, pr: str = MANIFEST) -> Request:
    return Request(pr=pr, bodies=bodies, states={FOLD: fold_state})


def test_replay_of_the_stall_order_is_held_while_the_fold_is_open() -> None:
    got = order_gate(_request({MANIFEST: DECLARED}, "OPEN"))
    assert (got.verdict, got.reason) == (Verdict.HOLD, Reason.PREDECESSOR_OPEN)
    assert got.waiting_on == (FOLD,)


def test_failing_control_without_the_declaration_today_s_order_lands_first() -> None:
    # The order of 2026-11-09: nothing says #1531 follows #5810, so the gate allows it. This is the red the
    # declaration removes; it is the only difference between this test and the one above.
    got = order_gate(
        _request({MANIFEST: "Remove the intelligence-api entries.\n"}, "OPEN")
    )
    assert (got.verdict, got.reason) == (Verdict.ALLOW, Reason.NO_DECLARED_ORDER)


def test_the_fold_is_released_once_the_manifest_half_may_follow() -> None:
    got = order_gate(_request({MANIFEST: DECLARED}, "MERGED"))
    assert (got.verdict, got.reason) == (Verdict.ALLOW, Reason.PREDECESSORS_MERGED)
    assert got.waiting_on == ()
    assert got.predecessors == (FOLD,)


def test_the_sibling_may_declare_the_order_instead_of_the_follower() -> None:
    bodies = {MANIFEST: "no declaration here", FOLD: SIBLING_DECLARES}
    held = order_gate(_request(bodies, "OPEN"))
    assert (held.verdict, held.waiting_on) == (Verdict.HOLD, (FOLD,))
    assert order_gate(_request(bodies, "MERGED")).verdict is Verdict.ALLOW


def test_the_fold_itself_is_never_held_by_its_follower() -> None:
    bodies = {MANIFEST: DECLARED, FOLD: SIBLING_DECLARES}
    got = order_gate(Request(pr=FOLD, bodies=bodies, states={MANIFEST: "OPEN"}))
    assert (got.verdict, got.reason) == (Verdict.ALLOW, Reason.NO_DECLARED_ORDER)


def test_owner_prefixed_refs_and_lists_and_case_are_read() -> None:
    body = f"lands-after: OmniNode-ai/{FOLD}, omnibase_infra#5839"
    got = order_gate(
        Request(pr=MANIFEST, bodies={MANIFEST: body}, states={FOLD: "MERGED"})
    )
    assert got.predecessors == (FOLD, "omnibase_infra#5839")
    assert (got.verdict, got.waiting_on) == (Verdict.HOLD, ("omnibase_infra#5839",))


def test_handler_entry_point_returns_the_same_result() -> None:
    request = _request({MANIFEST: DECLARED}, "OPEN")
    assert HandlerCrossRepoOrderGate().handle(request) == order_gate(request)


# ------------------------------------------------------------------ error chain
def test_error_a_predecessor_closed_unmerged_can_never_satisfy_the_order() -> None:
    got = order_gate(_request({MANIFEST: DECLARED}, "CLOSED"))
    assert (got.verdict, got.reason) == (Verdict.HOLD, Reason.PREDECESSOR_CLOSED)


@pytest.mark.parametrize("state", ["UNKNOWN", ""])
def test_error_an_unread_predecessor_holds_and_is_not_assumed_merged(
    state: str,
) -> None:
    got = order_gate(_request({MANIFEST: DECLARED}, state))
    assert (got.verdict, got.reason) == (Verdict.HOLD, Reason.PREDECESSOR_UNREAD)


def test_error_a_predecessor_missing_from_the_states_is_unread() -> None:
    got = order_gate(Request(pr=MANIFEST, bodies={MANIFEST: DECLARED}))
    assert (got.verdict, got.reason) == (Verdict.HOLD, Reason.PREDECESSOR_UNREAD)


def test_error_a_declaration_naming_no_ref_holds_instead_of_bypassing() -> None:
    got = order_gate(_request({MANIFEST: "Lands-After: the infra fold"}, "MERGED"))
    assert (got.verdict, got.reason) == (
        Verdict.HOLD,
        Reason.DECLARATION_UNPARSEABLE,
    )
    assert got.unparseable == ("the", "infra", "fold")


def test_error_a_request_with_an_undeclared_field_is_refused() -> None:
    with pytest.raises(ValueError, match="extra_field"):
        Request.model_validate(
            {"pr": MANIFEST, "bodies": {}, "states": {}, "extra_field": 1}
        )

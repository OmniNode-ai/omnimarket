# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session recovery acts only on the member's own lanes (two principals, each with a quiet lane).

A member session lists every dangling CLAIM but marks the other principal's as ``foreign``, runs the
reconciler in report mode only, and composes a closing TERMINAL, a relaunch CLAIM or a landing notice
only for a CLAIM its own principal wrote or for an unattributed child of its own session. An
operator session behaves as before. Unknown identity gets member restrictions, never operator.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_session_resume_compute import HandlerSessionResume
from omnimarket.nodes.node_session_resume_compute.handlers.ledger_grammar import (
    claim_row_digest,
)
from omnimarket.nodes.node_session_resume_compute.models import (
    ModelSessionResumeRequest,
    ModelSessionResumeResult,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
IDENTITY = REPO / "tests" / "fixtures" / "team_identity" / "valid.yaml"
LEDGER = REPO / "tests" / "fixtures" / "session_recovery" / "ROLLING_WORK_LEDGER.md"
T0 = "2026-10-29T09:00:00Z"
SINCE = "2026-10-29T00:00:00Z"
NOW = "2026-10-29T10:00:00Z"

MEMBER_A = [
    {"source": "github_auth", "value": "fixture-member-a-gh"},
    {"source": "broker_binding", "value": "fixture-member-a-dev"},
]
OPERATOR = [{"source": "github_auth", "value": "fixture-operator-gh"}]
ENVIRONMENT_ONLY = [{"source": "environment", "value": "fixture-member-a-dev"}]

OWN = {"sess-a", "lane-a", "child-a"}
FOREIGN = {"sess-b", "lane-b", "child-b", "orphan", "posing-child", "landing-sub-b"}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    roots = tmp_path / "roots"
    (roots / "team_identity").mkdir(parents=True)
    shutil.copyfile(IDENTITY, roots / "team_identity" / "overlay.yaml")
    ledger = tmp_path / "ledger" / "ROLLING_WORK_LEDGER.md"
    ledger.parent.mkdir()
    shutil.copyfile(LEDGER, ledger)
    return {"overlay_roots": [str(roots)], "ledger_path": str(ledger)}


def run(
    env: dict[str, Any], claims: list[dict[str, str]], **fields: Any
) -> ModelSessionResumeResult:
    request = ModelSessionResumeRequest.model_validate(
        {
            **env,
            "identity_claims": claims,
            "since": SINCE,
            "t0": T0,
            "now": NOW,
            **fields,
        }
    )
    return HandlerSessionResume().handle(request)


def stamp_of(lane: str) -> str:
    for line in LEDGER.read_text().splitlines():
        if f"| CLAIM | lane={lane} |" in line:
            return line.split(" | ", 1)[0]
    raise AssertionError(lane)


def close_request(lane: str, mode: str = "released", **extra: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "lane": "sess-a2",
        "dead_lane": lane,
        "dead_claim": stamp_of(lane),
        "mode": mode,
        "ticket": "OMN-9000099",
        "reason": "the operator released it",
    }
    if mode == "landed":
        spec.update(pr="omnimarket#901", squash="abcdef1234567")
    spec.update(extra)
    return {"operation": "close", "close": spec}


def relaunch_request(lane: str, parent: str | None = None) -> dict[str, Any]:
    return {
        "operation": "relaunch",
        "relaunch": {
            "dead_lane": lane,
            "dead_claim": stamp_of(lane),
            "dead_parent": parent,
            "landing_lane": "landing",
            "dispatcher": "sess-a2",
            "ticket": "OMN-9000099",
        },
    }


# (a) step 2 lists both principals' lanes; the other principal's are foreign.
def test_member_lists_every_lane_and_marks_the_other_principals_foreign(
    env: dict[str, Any],
) -> None:
    result = run(env, MEMBER_A, operation="dangling")
    assert result.status == "ok"
    assert result.scope.privileges == "team"
    assert result.scope.entry_id == "fixture-member-a"
    owners = {item.lane: item.owner for item in result.dangling}
    assert set(owners) == OWN | FOREIGN, "both principals' quiet lanes stay listed"
    assert {lane for lane, owner in owners.items() if owner == "own"} == OWN
    assert {lane for lane, owner in owners.items() if owner == "foreign"} == FOREIGN
    assert all(item.state == "QUIET" for item in result.dangling)
    by_lane = {item.lane: item for item in result.dangling}
    assert by_lane["lane-b"].principal == "fixture-member-b-dev"
    assert by_lane["child-a"].principal == ""
    assert "done-a" not in owners, "a closed CLAIM is not dangling"
    assert "foreign=6" in result.summary
    assert result.prs == ("omnimarket#901", "omnimarket#902")


# (a) step 3a: a member runs the reconciler in report mode only.
def test_member_runs_the_reconciler_in_report_mode_only(env: dict[str, Any]) -> None:
    assert run(env, MEMBER_A, operation="dangling").reconciler_mode == "report"
    assert (
        run(env, MEMBER_A, operation="dangling", dry_run=True).reconciler_mode
        == "report"
    )


# (a) steps 3 and 4: the member closes and relaunches its own lanes and its session's children.
@pytest.mark.parametrize("lane", sorted(OWN))
def test_member_closes_and_relaunches_its_own_lanes(
    env: dict[str, Any], lane: str
) -> None:
    closed = run(env, MEMBER_A, **close_request(lane))
    assert closed.status == "ok", closed.reason
    assert closed.row is not None
    assert (
        f"| TERMINAL | lane={lane} | closed-by=sess-a2 | closes-CLAIM={stamp_of(lane)}-{lane} |"
        in closed.row
    )
    relaunched = run(env, MEMBER_A, **relaunch_request(lane))
    assert relaunched.status == "ok"
    assert relaunched.route == "relaunch"
    assert relaunched.row is not None
    assert f"supersedes-claim={stamp_of(lane)}-{lane}" in relaunched.row
    assert "parent=sess-a2" in relaunched.row


# Safety: a member session never composes a CLOSE, RELAUNCH or MSG for another principal's CLAIM.
@pytest.mark.parametrize("lane", sorted(FOREIGN))
def test_member_session_never_composes_a_row_for_a_foreign_claim(
    env: dict[str, Any], lane: str
) -> None:
    after_msg = (
        "2026-10-29T09:30:00Z | MSG | from=sess-a2 | to=landing | id=2026-10-29T09:30:00Z-sess-a2 | "
        "ticket=OMN-9000004 | repo=omnimarket | pr=omnimarket#902 | head=abcdef1 | needs=land | hand off"
    )
    attempts = [
        close_request(lane, "released"),
        close_request(lane, "landed"),
        close_request(lane, "after_msg", after_msg=after_msg),
        relaunch_request(lane),
        relaunch_request(
            lane, parent="landing"
        ),  # the landing route would send a notice MSG
    ]
    for attempt in attempts:
        result = run(env, MEMBER_A, **attempt)
        assert result.status == "refused", (attempt, result)
        assert result.row is None
        assert result.route is None
        assert "another principal" in (result.reason or "")


def test_member_is_refused_a_foreign_claim_named_by_its_token(
    env: dict[str, Any],
) -> None:
    line = next(
        row
        for row in LEDGER.read_text().splitlines()
        if "| CLAIM | lane=lane-b |" in row
    )
    token = f"LCT1-100-4-{claim_row_digest(line)}-2026-10-29T08:05:01Z"
    result = run(env, MEMBER_A, **close_request("lane-b", dead_claim=token))
    assert result.status == "refused"
    assert result.row is None
    own_line = next(
        row
        for row in LEDGER.read_text().splitlines()
        if "| CLAIM | lane=lane-a |" in row
    )
    own_token = f"LCT1-100-3-{claim_row_digest(own_line)}-2026-10-29T08:05:01Z"
    own = run(env, MEMBER_A, **close_request("lane-a", dead_claim=own_token))
    assert own.status == "ok"
    assert own.row is not None
    assert f"closes-CLAIM={own_token}" in own.row


def test_member_is_refused_a_claim_it_cannot_find(env: dict[str, Any]) -> None:
    result = run(
        env, MEMBER_A, **close_request("lane-a", dead_claim="2026-10-29T08:59:59Z")
    )
    assert result.status == "refused"
    assert result.row is None
    assert "cannot be verified" in (result.reason or "")


# Unknown identity: member restrictions, never operator; it owns nothing and so acts on nothing.
@pytest.mark.parametrize(
    "claims", [[], ENVIRONMENT_ONLY, [{"source": "github_auth", "value": "nobody"}]]
)
def test_unknown_identity_gets_member_restrictions_and_owns_nothing(
    env: dict[str, Any], claims: list[dict[str, str]]
) -> None:
    listing = run(env, claims, operation="dangling")
    assert listing.scope.privileges == "team"
    assert listing.scope.principals == ()
    assert listing.scope.diagnostic
    assert listing.reconciler_mode == "report"
    assert {item.owner for item in listing.dangling} == {"foreign"}
    for lane in sorted(OWN):
        assert run(env, claims, **close_request(lane)).status == "refused"
        assert run(env, claims, **relaunch_request(lane)).status == "refused"


def test_missing_declaration_gets_member_restrictions(
    env: dict[str, Any], tmp_path: Path
) -> None:
    env = {**env, "overlay_roots": [str(tmp_path / "empty")]}
    result = run(env, OPERATOR, operation="dangling")
    assert result.scope.privileges == "team"
    assert result.reconciler_mode == "report"
    assert run(env, OPERATOR, **close_request("lane-a")).status == "refused"


# (d) an operator session behaves as before.
def test_operator_session_behaves_as_before(env: dict[str, Any]) -> None:
    result = run(env, OPERATOR, operation="dangling")
    assert result.scope.privileges == "operator"
    assert result.reconciler_mode == "apply"
    assert (
        run(env, OPERATOR, operation="dangling", dry_run=True).reconciler_mode
        == "report"
    )
    assert {item.owner for item in result.dangling} == {"own"}
    assert {item.lane for item in result.dangling} == OWN | FOREIGN
    for lane in sorted(FOREIGN - {"landing-sub-b"}):
        assert run(env, OPERATOR, **close_request(lane)).status == "ok"
        assert run(env, OPERATOR, **relaunch_request(lane)).route == "relaunch"
    routed = run(env, OPERATOR, **relaunch_request("landing-sub-b", parent="landing"))
    assert routed.status == "ok"
    assert routed.route == "landing-lane"
    assert routed.row is None
    unknown = run(
        env, OPERATOR, **close_request("lane-a", dead_claim="2026-10-29T08:59:59Z")
    )
    assert unknown.status == "ok", "an operator closes by reference, as before"


def test_close_and_supersede_rows_close_claims(env: dict[str, Any]) -> None:
    ledger = Path(env["ledger_path"])
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(
            "2026-10-29T09:10:00Z | CLAIM | lane=lane-a-r | principal=fixture-member-a-dev | "
            "ticket=OMN-9000003 | parent=sess-a | supersedes-claim=2026-10-29T08:05:00Z-lane-a | "
            "worktree=none | relaunch\n"
            "2026-10-29T09:11:00Z | TERMINAL | lane=child-a | closed-by=sess-a | "
            "closes-CLAIM=2026-10-29T08:06:00Z-child-a | ticket=OMN-9000005 | outcome=released | "
            "delegation=na:fixture | released\n"
        )
    lanes = {item.lane for item in run(env, MEMBER_A, operation="dangling").dangling}
    assert "lane-a" not in lanes
    assert "child-a" not in lanes
    assert "lane-a-r" in lanes


def test_missing_ledger_is_an_error(env: dict[str, Any], tmp_path: Path) -> None:
    result = run(
        {**env, "ledger_path": str(tmp_path / "absent.md")},
        MEMBER_A,
        operation="dangling",
    )
    assert result.status == "error"

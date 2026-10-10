# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A PR red only on the change-control companion cascade waits for its companion (ported from the controller).

Read live on 2026-09-30: 28 of 86 landing workers that day returned ``external_blocker upstream_open`` on an open
companion, because the red class called a PR whose only reds were the companion cascade a product red while its
companion was open. Such a PR is ``companion_wait``; a companion that is open and red on its own checks gets the
worker instead of the product PR.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.unit.nodes.node_pr_lifecycle_triage_compute import (
    landing_rules_adapter as lf,
)

pytestmark = pytest.mark.unit

CASCADE = ["CI Summary", "OCC Companion Merged Gate", "occ-preflight eligibility"]
COMP = "onex_change_control#11886"
PR = "omnibase_infra#4333"


def _runs(names: list[str]) -> list[list[str]]:
    return [[n, "completed", "failure", "2026-09-30T06:58:00Z"] for n in names]


def _rec(
    red: list[str], head: str = "a" * 40, companion: int | None = 11886
) -> dict[str, Any]:
    return {
        "ci": {"sha": head, "red": red, "runs": _runs(red)},
        "facts": {
            "created_at": "2026-09-01T00:00:00Z",
            "evidence_companion": companion,
            "head_sha": head,
        },
    }


def _comp_rec(
    state: str, red: list[str] | None = None, head: str = "c" * 40
) -> dict[str, Any]:
    return {
        "cls": "companion-pending",
        "ci": {"sha": head, "red": red or [], "runs": _runs(red or [])},
        "facts": {
            "state": state,
            "head_sha": head,
            "created_at": "2026-09-30T06:50:00Z",
        },
    }


def _live(**kw: Any) -> SimpleNamespace:
    base = {
        "state": "OPEN",
        "head": "a" * 40,
        "draft": False,
        "mergeable": "MERGEABLE",
        "merge_state": "CLEAN",
        "rollup": "FAILURE",
        "armed": False,
        "labels": (),
        "base": "dev",
        "author": "ops",
        "author_is_bot": False,
        "queue_rejected": False,
    }
    return SimpleNamespace(**{**base, **kw})


def test_a_receipt_gate_red_with_an_open_companion_is_companion_wait() -> None:
    rec = _rec([*CASCADE, "verify / verify"])
    records = {COMP: _comp_rec("OPEN")}
    assert lf.companion_wait_of(PR, rec, _live(), records) == {
        "companion": COMP,
        "companion_red": [],
    }


def test_a_hostile_review_red_beside_the_cascade_is_companion_wait() -> None:
    rec = _rec([*CASCADE, "Hostile Review Gate", "verify / verify"])
    assert lf.companion_wait_of(PR, rec, _live(), {COMP: _comp_rec("OPEN")}) == {
        "companion": COMP,
        "companion_red": [],
    }


def test_a_cascade_red_with_an_open_companion_is_companion_wait() -> None:
    cw = lf.companion_wait_of(PR, _rec(CASCADE), _live(), {COMP: _comp_rec("OPEN")})
    assert cw == {"companion": COMP, "companion_red": []}


def test_a_merged_companion_leaves_the_cascade_class() -> None:
    records = {COMP: _comp_rec("MERGED")}
    assert lf.companion_wait_of(PR, _rec(CASCADE), _live(), records) is None
    runs = _runs(CASCADE)
    assert lf.classify_red(CASCADE, runs, companion_merged=True) == "cascade"


def test_a_real_red_beside_the_cascade_is_the_prs_own() -> None:
    rec = _rec([*CASCADE, "Tests (Split 7/15)"])
    assert lf.companion_wait_of(PR, rec, _live(), {COMP: _comp_rec("OPEN")}) is None
    assert (
        lf.classify_red(rec["ci"]["red"], rec["ci"]["runs"], companion_merged=False)
        == "product"
    )


def test_no_companion_or_an_unknown_one_fails_closed_to_a_worker() -> None:
    assert lf.companion_wait_of(PR, _rec(CASCADE, companion=None), _live(), {}) is None
    assert lf.companion_wait_of(PR, _rec(CASCADE), _live(), {}) is None


def test_a_conflicting_head_still_gets_its_conflict_worker() -> None:
    lv = _live(mergeable="CONFLICTING", merge_state="DIRTY")
    assert (
        lf.companion_wait_of(PR, _rec(CASCADE), lv, {COMP: _comp_rec("OPEN")}) is None
    )


def test_a_red_read_at_an_older_head_is_not_companion_wait() -> None:
    rec = _rec(CASCADE, head="b" * 40)
    assert lf.companion_wait_of(PR, rec, _live(), {COMP: _comp_rec("OPEN")}) is None


def test_a_green_or_unread_live_head_is_not_companion_wait() -> None:
    records = {COMP: _comp_rec("OPEN")}
    assert (
        lf.companion_wait_of(PR, _rec(CASCADE), _live(rollup="SUCCESS"), records)
        is None
    )
    assert lf.companion_wait_of(PR, _rec(CASCADE), None, records) is None


def test_a_pr_that_is_its_own_companion_does_not_wait_on_itself() -> None:
    records = {COMP: _comp_rec("OPEN")}
    assert lf.companion_wait_of(COMP, _rec(CASCADE), _live(), records) is None


def test_the_companions_own_reds_come_from_its_head_without_the_summary() -> None:
    records = {COMP: _comp_rec("OPEN", red=["CI Summary", "Pre-commit"])}
    cw = lf.companion_wait_of(PR, _rec(CASCADE), _live(), records)
    assert cw == {"companion": COMP, "companion_red": ["Pre-commit"]}
    stale = {
        COMP: {
            **_comp_rec("OPEN", red=["Pre-commit"]),
            "ci": {"sha": "d" * 40, "red": ["Pre-commit"]},
        }
    }
    cw_stale = lf.companion_wait_of(PR, _rec(CASCADE), _live(), stale)
    assert cw_stale is not None
    assert cw_stale["companion_red"] == []


def test_the_dod_verify_always_pass_red_is_not_companion_wait_in_cause_mode() -> None:
    dod = "repo-evidence / dod-verify"
    red = ["CI Summary", "OCC Preflight Dependency", "occ-preflight / eligibility", dod]
    rec = _rec(red)
    records = {COMP: _comp_rec("OPEN")}
    ann = {
        dod: "bound test also passes at the merge base: the control did not fail (always-pass)"
    }
    assert lf.companion_wait_of(PR, rec, _live(), records) is not None
    assert lf.companion_wait_of(PR, rec, _live(), records, ann) is None
    for text in (
        "no test-side change: the bound tests cannot be shown to fail at the merge base",
        "",
    ):
        assert lf.companion_wait_of(PR, rec, _live(), records, {dod: text}) is not None

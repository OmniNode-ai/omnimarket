# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pre-registered sample rules of the shadow-review tick (OMN-20422)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_shadow_review_effect.handlers.shadow_selection import (
    disqualification,
    select_candidates,
    stratum_of,
)
from omnimarket.nodes.node_shadow_review_effect.models.model_shadow_review import (
    EnumShadowDecision,
    EnumShadowStratum,
    ModelShadowReviewCandidate,
    ModelShadowReviewPolicy,
    ModelShadowSelection,
)

WINDOW = "2026-10-09T10:00:00Z"


def pr(
    repo: str, n: int, created: str = "2026-10-09T11:00:00Z", **kw: object
) -> ModelShadowReviewCandidate:
    fields: dict[str, object] = {
        "repo": repo,
        "number": n,
        "created_at": created,
        "head_sha": "a" * 40,
        "head_ref": f"branch-{n}",
        "base": "dev",
        "title": f"feat(OMN-1): change {n}",
    }
    fields.update(kw)
    return ModelShadowReviewCandidate.model_validate(fields)


def decisions(rows: tuple[ModelShadowSelection, ...]) -> dict[str, tuple[str, str]]:
    return {r.key: (r.decision.value, r.reason) for r in rows}


POLICY = ModelShadowReviewPolicy(window_start=WINDOW, max_reviews_per_tick=10)


def test_strata_follow_the_preregistration_and_consent() -> None:
    assert stratum_of("omnimarket", POLICY) is EnumShadowStratum.PUBLIC
    assert stratum_of("omnibase_internal", POLICY) is EnumShadowStratum.PRIVATE
    assert stratum_of("knowledge-base-internal", POLICY) is EnumShadowStratum.EXCLUDED
    # not-known-public repositories are outside both strata
    assert stratum_of("omnibase_core", POLICY) is EnumShadowStratum.EXCLUDED


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (pr("knowledge-base-internal", 1), "excluded-repository"),
        (pr("omniweb", 2), "not-known-public"),
        (pr("omnimarket", 3, title="chore(deps): bump x"), "dependency-bump"),
        (pr("omnimarket", 4, title="build(deps-dev): y"), "dependency-bump"),
        (pr("omnimarket", 5, title="Bump pyyaml from 6 to 7"), "dependency-bump"),
        (
            pr("omnimarket", 6, title="refresh uv.lock", author_is_bot=True),
            "lockfile-refresh",
        ),
        (
            pr("onex_change_control", 7, watcher_class="orphan-companion"),
            "change-control-companion",
        ),
        (
            pr("onex_change_control", 8, evidence_companion="omnimarket#1"),
            "change-control-companion",
        ),
        (
            pr("onex_change_control", 9, author="onexbot-occ-writer[bot]"),
            "change-control-companion",
        ),
    ],
)
def test_disqualified_prs_are_never_reviewed(
    candidate: ModelShadowReviewCandidate, reason: str
) -> None:
    assert disqualification(candidate, POLICY) == reason
    rows = select_candidates(
        [candidate], POLICY, already_seen=frozenset(), public_reviewed=0
    )
    assert decisions(rows) == {candidate.key: ("skip", reason)}


def test_draft_and_ordinary_occ_prs_qualify() -> None:
    assert disqualification(pr("omnimarket", 1, draft=True), POLICY) is None
    assert (
        disqualification(pr("onex_change_control", 2, watcher_class="pending"), POLICY)
        is None
    )


def test_no_window_reviews_nothing() -> None:
    rows = select_candidates(
        [pr("omnimarket", 1)],
        ModelShadowReviewPolicy(),
        already_seen=frozenset(),
        public_reviewed=0,
    )
    assert decisions(rows) == {"omnimarket#1": ("skip", "window-not-open")}


def test_window_reviewed_once_ordered_and_bounded() -> None:
    cands = [
        pr("omnimarket", 3, created="2026-10-09T12:00:00Z"),
        pr("omnimarket", 1, created="2026-10-09T09:59:59Z"),
        pr("omnibase_internal", 2, created="2026-10-09T11:00:00Z"),
        pr("omnimarket", 4, created="2026-10-09T13:00:00Z"),
        pr("omnimarket", 5, created="2026-10-09T10:30:00Z"),
    ]
    policy = ModelShadowReviewPolicy(window_start=WINDOW, max_reviews_per_tick=2)
    rows = select_candidates(
        cands, policy, already_seen=frozenset({"omnimarket#5"}), public_reviewed=0
    )
    assert [r.key for r in rows] == [
        "omnimarket#1",
        "omnimarket#5",
        "omnibase_internal#2",
        "omnimarket#3",
        "omnimarket#4",
    ]
    assert decisions(rows) == {
        "omnimarket#1": ("skip", "created-before-window"),
        "omnimarket#5": ("skip", "already-in-store"),
        "omnibase_internal#2": ("review", "qualifying"),
        "omnimarket#3": ("review", "qualifying"),
        "omnimarket#4": ("skip", "deferred-next-tick"),
    }


def test_public_cap_closes_the_sample() -> None:
    policy = ModelShadowReviewPolicy(
        window_start=WINDOW, public_cap=60, max_reviews_per_tick=5
    )
    rows = select_candidates(
        [pr("omnimarket", 1), pr("omnibase_internal", 2)],
        policy,
        already_seen=frozenset(),
        public_reviewed=60,
    )
    assert {r.reason for r in rows} == {"sample-complete"}
    assert all(r.decision is EnumShadowDecision.SKIP for r in rows)


def test_only_ignores_window_but_keeps_scope() -> None:
    cands = [
        pr("omnimarket", 1, created="2026-10-01T00:00:00Z"),
        pr("knowledge-base-internal", 2),
        pr("omnimarket", 3),
    ]
    rows = select_candidates(
        cands,
        ModelShadowReviewPolicy(),
        already_seen=frozenset(),
        public_reviewed=0,
        only=frozenset({"omnimarket#1", "knowledge-base-internal#2"}),
    )
    assert decisions(rows) == {
        "omnimarket#1": ("review", "qualifying"),
        "knowledge-base-internal#2": ("skip", "excluded-repository"),
    }

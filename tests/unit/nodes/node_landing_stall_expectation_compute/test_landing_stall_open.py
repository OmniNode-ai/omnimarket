# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stall rule over snapshot rows: one required context red on a share of the open pull requests.

The replay fixtures are the repository-status rows the ledger holds: 2026-10-31, when
one required context went red on nearly every open pull request of one repository and
the first alarm came 2h37m after the cause, and 12 ordinary days from 2026-10-16 to
2026-10-30. The thresholds are read from the node's overlay, never a literal in code
(the fixture overlay carries the ruling's values).
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_landing_stall_expectation_compute.handlers.handler_landing_stall_open import (
    HandlerLandingStallOpen,
)
from omnimarket.nodes.node_landing_stall_expectation_compute.models.model_landing_stall_fold import (
    ModelLandingStallFoldRequest,
)
from omnimarket.nodes.node_landing_stall_expectation_compute.models.model_landing_stall_open import (
    EnumStallPhase,
    ModelLandingStallOpenRequest,
    ModelLandingStallOpenResult,
    ModelLandingStallThresholds,
    ModelRepoContextSnapshot,
    ModelStallContextRef,
)
from omnimarket.nodes.node_landing_stall_expectation_compute.overlay import (
    NODE_NAME,
    load_stall_thresholds,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "landing_stall"
OVERLAY_ROOT = FIXTURES / "overlay_root"
NODE_SRC = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_landing_stall_expectation_compute"
)
REPO = "OmniNode-ai/omnibase_infra"
DOD = "repo-evidence / dod-verify"
OPEN_BY = datetime(2026, 10, 31, 10, 45, tzinfo=UTC)


def _snapshots(name: str) -> tuple[ModelRepoContextSnapshot, ...]:
    raw = json.loads((FIXTURES / name).read_text("utf-8"))
    return tuple(ModelRepoContextSnapshot.model_validate(s) for s in raw["snapshots"])


def _ruling_thresholds(monkeypatch: pytest.MonkeyPatch) -> ModelLandingStallThresholds:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(OVERLAY_ROOT))
    return load_stall_thresholds()


def _run(
    snapshots: tuple[ModelRepoContextSnapshot, ...],
    thresholds: ModelLandingStallThresholds,
    eligible: tuple[ModelStallContextRef, ...] = (),
) -> ModelLandingStallOpenResult:
    return HandlerLandingStallOpen().handle(
        ModelLandingStallOpenRequest(
            snapshots=snapshots, thresholds=thresholds, eligible_repairs=eligible
        )
    )


def test_stall_replay_2026_10_31_opens_by_10_45(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thresholds = _ruling_thresholds(monkeypatch)
    snaps = _snapshots("replay_2026_10_31.json")
    before = _run(snaps[:1], thresholds)
    assert before.stalls == ()
    result = _run(snaps[:2], thresholds)
    assert [(s.repo, s.context) for s in result.stalls] == [(REPO, DOD)]
    stall = result.stalls[0]
    assert stall.opened_at == datetime(2026, 10, 31, 10, 39, 33, tzinfo=UTC)
    assert stall.opened_at <= OPEN_BY
    assert stall.phase is EnumStallPhase.OPEN
    assert (stall.opened_red_prs, stall.opened_open_prs) == (20, 23)


def test_stall_replay_2026_10_31_escalates_at_the_overlay_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thresholds = _ruling_thresholds(monkeypatch)
    snaps = _snapshots("replay_2026_10_31.json")
    opened = datetime(2026, 10, 31, 10, 39, 33, tzinfo=UTC)

    at_exception = _run(snaps[:3], thresholds).stalls
    assert len(at_exception) == 1
    assert at_exception[0].phase is EnumStallPhase.EXCEPTION_DUE
    assert at_exception[0].exception_due_at == opened + timedelta(minutes=60)
    assert at_exception[0].question_due_at == opened + timedelta(minutes=120)

    at_question = _run(snaps[:4], thresholds).stalls
    assert [s.phase for s in at_question] == [EnumStallPhase.QUESTION_DUE]

    repair = ModelStallContextRef(repo=REPO, context=DOD)
    repaired = _run(snaps[:4], thresholds, eligible=(repair,)).stalls
    assert [s.phase for s in repaired] == [EnumStallPhase.EXCEPTION_DUE]

    whole = _run(snaps, thresholds).stalls
    assert len(whole) == 1, "one stall per context, however many snapshots read it"
    assert whole[0].phase is EnumStallPhase.QUESTION_DUE
    assert (whole[0].red_prs, whole[0].open_prs) == (33, 35)


def test_stall_replay_2026_10_31_old_fold_cannot_see_the_rows() -> None:
    """The old fold reads controller transitions only: snapshot rows are not input to it."""
    with pytest.raises(ValueError, match="snapshots"):
        ModelLandingStallFoldRequest.model_validate(
            {"transitions": [], "routes": [], "snapshots": [{"repo": REPO}]}
        )


def test_stall_replay_14_ordinary_days_open_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thresholds = _ruling_thresholds(monkeypatch)
    snaps = _snapshots("ordinary_days_2026_10.json")
    days = {s.observed_at.date() for s in snaps}
    assert len(days) == len(snaps) == 12
    assert _run(snaps, thresholds).stalls == ()


def test_stall_replay_ordinary_days_zero_is_not_vacuous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thresholds = _ruling_thresholds(monkeypatch)
    snaps = _snapshots("ordinary_days_2026_10.json")
    loose = thresholds.model_copy(update={"share_percent": 50})
    assert _run(snaps, loose).stalls, "a lower share must open stalls on the same rows"
    # The rollup is red wherever anything is: 7 of 8 open on 2026-10-28.
    no_rollup_rule = thresholds.model_copy(update={"aggregate_contexts": ()})
    opened = _run(snaps, no_rollup_rule).stalls
    assert any(s.context == "CI Summary" for s in opened)


def test_stall_replay_a_recorded_repo_wide_red_on_2026_10_23_opens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner Routing Audit failed on 17 of 19 open pull requests; the rule calls that a stall."""
    thresholds = _ruling_thresholds(monkeypatch)
    snaps = _snapshots("recorded_repo_wide_red_2026_10_23.json")
    result = _run(snaps, thresholds)
    assert [s.context for s in result.stalls] == [
        "runner-routing-audit / Runner Routing Audit"
    ]


def test_stall_closes_after_consecutive_clear_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thresholds = _ruling_thresholds(monkeypatch)
    base = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)

    def snap(minute: int, red: int) -> ModelRepoContextSnapshot:
        return ModelRepoContextSnapshot(
            observed_at=base + timedelta(minutes=minute),
            repo=REPO,
            open_prs=10,
            red_by_context={DOD: red},
        )

    flap = _run((snap(0, 9), snap(5, 0), snap(10, 9)), thresholds).stalls
    assert [s.phase for s in flap] == [EnumStallPhase.OPEN]
    assert flap[0].closed_at is None
    assert flap[0].opened_at == base

    cleared = _run((snap(0, 9), snap(5, 0), snap(10, 0)), thresholds).stalls
    assert [s.phase for s in cleared] == [EnumStallPhase.CLOSED]
    assert cleared[0].closed_at == base + timedelta(minutes=10)

    reopened = _run(
        (snap(0, 9), snap(5, 0), snap(10, 0), snap(15, 9)), thresholds
    ).stalls
    assert [s.phase for s in reopened] == [EnumStallPhase.CLOSED, EnumStallPhase.OPEN]
    assert reopened[1].opened_at == base + timedelta(minutes=15)


def test_stall_boundaries_are_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    thresholds = _ruling_thresholds(monkeypatch)
    at = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)

    def snap(open_prs: int, red: int) -> ModelRepoContextSnapshot:
        return ModelRepoContextSnapshot(
            observed_at=at, repo=REPO, open_prs=open_prs, red_by_context={DOD: red}
        )

    assert len(_run((snap(5, 4),), thresholds).stalls) == 1, (
        "4 of 5 is exactly 80 percent"
    )
    assert _run((snap(5, 3),), thresholds).stalls == ()
    assert _run((snap(4, 4),), thresholds).stalls == (), "fewer than five open"


def test_thresholds_from_overlay_change_the_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {
            "share_percent": 50,
            "min_open_prs": 3,
            "exception_minutes": 10,
            "question_minutes": 20,
            "clear_snapshots": 1,
            "aggregate_contexts": [],
        },
    )
    thresholds = load_stall_thresholds()
    assert (
        thresholds.share_percent,
        thresholds.min_open_prs,
        thresholds.exception_minutes,
        thresholds.question_minutes,
        thresholds.clear_snapshots,
    ) == (50, 3, 10, 20, 1)
    base = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
    snaps = tuple(
        ModelRepoContextSnapshot(
            observed_at=base + timedelta(minutes=m),
            repo=REPO,
            open_prs=4,
            red_by_context={DOD: 2},
        )
        for m in (0, 25)
    )
    stalls = _run(snaps, thresholds).stalls
    assert len(stalls) == 1, "2 of 4 opens at 50 percent and three open pull requests"
    assert stalls[0].exception_due_at == base + timedelta(minutes=10)
    assert stalls[0].question_due_at == base + timedelta(minutes=20)
    assert stalls[0].phase is EnumStallPhase.QUESTION_DUE
    # The same rows under the ruling's values open nothing.
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(OVERLAY_ROOT))
    assert _run(snaps, load_stall_thresholds()).stalls == ()


def test_thresholds_from_overlay_refuses_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError):
        load_stall_thresholds()


def test_thresholds_from_overlay_refuses_a_partial_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, {"share_percent": 80})
    with pytest.raises(NodeOverlayError):
        load_stall_thresholds()


def test_thresholds_from_overlay_none_is_a_literal_in_code() -> None:
    """No threshold of the ruling (80, 5, 60, 120) appears as a number in the new code."""
    forbidden = {5, 60, 80, 120}
    for relative in (
        "handlers/handler_landing_stall_open.py",
        "models/model_landing_stall_open.py",
        "overlay.py",
    ):
        tree = ast.parse((NODE_SRC / relative).read_text("utf-8"))
        numbers = {
            n.value
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, int)
            and not isinstance(n.value, bool)
        }
        assert not numbers & forbidden, (relative, numbers & forbidden)

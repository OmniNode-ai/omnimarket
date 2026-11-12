# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay of the dev host's runtime starts of 2026-11-10 through the start-count verdict.

The fixture is the docker journal of the dev host (plan evidence E1.1): it recorded a dev
runtime start at 05:19:59, 05:28:10, 05:44:03, 05:50:29, 06:05:20, 06:10:18, 06:26:23 and
06:31:48Z. Each consecutive pair is one roll: the compose recreate, then the branch-install
reapply -- four rolls, two starts each, under the declared one start per roll. It does not
record the job ids of those rolls, so each roll is labeled by its first start's
time (``roll-05:19:59Z`` and so on); only the times are evidence, the ids are this
replay's labels.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid5

import pytest

from omnimarket.nodes.node_dev_roll_verify_compute.handlers import (
    decide_dev_roll_verify,
)
from omnimarket.nodes.node_dev_roll_verify_compute.models import (
    EnumDevRollVerdict,
    EnumDevStartCause,
    ModelDevRollVerified,
    ModelDevRollVerifyFacts,
    ModelDevRollVerifyPolicy,
    ModelDevRuntimeStarted,
)
from tests.unit.nodes.node_dev_roll_verify_compute.conftest import facts

pytestmark = pytest.mark.unit

_JOURNAL_TIMES: tuple[str, ...] = (
    "05:19:59",
    "05:28:10",
    "05:44:03",
    "05:50:29",
    "06:05:20",
    "06:10:18",
    "06:26:23",
    "06:31:48",
)
_DAY = "2026-11-10"
_NAMESPACE = UUID("5e2a1c44-7b1d-4e3a-9c6f-0a1b2c3d4e5f")
# The journal's recreate-to-reapply gaps span this window (evidence E1.1).
GAP_MIN_SECONDS = 298
GAP_MAX_SECONDS = 491


def _t(text: str) -> datetime:
    return datetime.fromisoformat(f"{_DAY}T{text}+00:00")


def _label(first: str) -> str:
    return f"roll-{first}Z"


def _pairs() -> list[tuple[str, str]]:
    """The journal's starts folded into (recreate, reapply) pairs, one per roll."""
    return list(zip(_JOURNAL_TIMES[::2], _JOURNAL_TIMES[1::2], strict=True))


def _events(
    pair: tuple[str, str],
) -> tuple[ModelDevRuntimeStarted, ModelDevRuntimeStarted]:
    label = _label(pair[0])
    return (
        ModelDevRuntimeStarted(
            event_id=uuid5(_NAMESPACE, f"{label}/roll_recreate"),
            job_id=label,
            runtime_lane="dev",
            started_at=_t(pair[0]),
            cause=EnumDevStartCause.ROLL_RECREATE,
        ),
        ModelDevRuntimeStarted(
            event_id=uuid5(_NAMESPACE, f"{label}/branch_install_reapply"),
            job_id=label,
            runtime_lane="dev",
            started_at=_t(pair[1]),
            cause=EnumDevStartCause.BRANCH_INSTALL_REAPPLY,
        ),
    )


def _facts(pair: tuple[str, str]) -> ModelDevRollVerifyFacts:
    recreate, reapply = _events(pair)
    return facts(
        (recreate, reapply),
        job_id=_label(pair[0]),
        roll_finished_at=_t(pair[1]) + timedelta(seconds=60),
    )


def test_the_journal_holds_eight_starts_making_four_rolls() -> None:
    pairs = _pairs()
    assert [_label(first) for first, _second in pairs] == [
        "roll-05:19:59Z",
        "roll-05:44:03Z",
        "roll-06:05:20Z",
        "roll-06:26:23Z",
    ], "the journal (evidence E1.1) or the pairing into rolls changed shape"
    for pair in pairs:
        recreate, reapply = _events(pair)
        assert recreate.cause is EnumDevStartCause.ROLL_RECREATE, (
            f"{_label(pair[0])}: the journal's first start (evidence E1.1) is no "
            "longer a compose recreate"
        )
        assert reapply.cause is EnumDevStartCause.BRANCH_INSTALL_REAPPLY, (
            f"{_label(pair[0])}: the journal's second start (evidence E1.1) is no "
            "longer a branch-install reapply"
        )


def test_each_rolled_pair_of_2026_11_10_verdicts_too_many_starts(
    policy: ModelDevRollVerifyPolicy,
) -> None:
    for pair in _pairs():
        label = _label(pair[0])
        result = decide_dev_roll_verify(_facts(pair), policy)
        assert isinstance(result, ModelDevRollVerified), (
            f"{label}: the verifier refused instead of verdicting; the journal's "
            "facts (evidence E1.1) or ModelDevRollVerifyFacts (ours) changed shape"
        )
        assert result.verdict is EnumDevRollVerdict.TOO_MANY_STARTS, (
            f"{label}: the verdict rules (ours) or the journal's two starts per roll "
            "(evidence E1.1) changed shape"
        )
        assert result.starts_counted == 2, (
            f"{label}: the handler's count (ours) or the journal's two starts per "
            "pair (evidence E1.1) changed shape"
        )
        assert result.declared_starts == 1, (
            "declared_starts_per_roll (the overlay's side) moved off one start per roll"
        )
        assert result.reapply_starts == 1, (
            f"{label}: the reapply count (ours) or the journal's one reapply per "
            "pair (evidence E1.1) changed shape"
        )
        assert result.starts_by_cause == {
            "branch_install_reapply": 1,
            "roll_recreate": 1,
        }, (
            f"{label}: EnumDevStartCause values (ours) or the journal's cause pair "
            "(evidence E1.1) changed shape"
        )
        assert result.first_start_at == _t(pair[0]), (
            f"{label}: the verifier's first start (ours) or the journal's recreate "
            "time (evidence E1.1) changed shape"
        )
        assert result.last_start_at == _t(pair[1]), (
            f"{label}: the verifier's last start (ours) or the journal's reapply "
            "time (evidence E1.1) changed shape"
        )


def test_the_gap_inside_each_pair_is_a_recreate_then_a_reapply() -> None:
    gaps: list[int] = []
    for first, second in _pairs():
        gap = int((_t(second) - _t(first)).total_seconds())
        assert GAP_MIN_SECONDS <= gap <= GAP_MAX_SECONDS, (
            f"{_label(first)}: the journal gap {gap}s (evidence E1.1) left the "
            f"{GAP_MIN_SECONDS}..{GAP_MAX_SECONDS}s recreate-then-reapply window"
        )
        gaps.append(gap)
    print(f"REPLAY rolls=4 too_many_starts=4 gap_seconds={gaps}")

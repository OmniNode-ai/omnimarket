# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A scoped RELEASE of a fixer HOLD lifts only the PRs it names, never the repository."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.handlers.work_ledger_fixer_holds import (
    fixer_hold_reasons,
    fixer_hold_released_prs,
)
from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow
from tests.unit.nodes.node_pr_landing_decision_compute.test_landing_decision_causes import (
    REPO,
    CauseWorld,
    Pr,
    dispatched,
)

NOW = datetime(2026, 11, 2, 9, 0, 0, tzinfo=UTC)
HOLD_ID = "2026-10-26T17:24:04Z-orchestrator-31d9"
HOLD = (
    f"2026-10-26T17:24:04Z | HOLD | lane=orchestrator-31d9 | id={HOLD_ID} | to=all"
    " | until=2026-11-09T00:00:00Z | scope=fixer | repo=held_repo | text"
)


def _row(ts: str, rtype: str, text: str) -> ModelLandingLedgerRow:
    return ModelLandingLedgerRow(ts=ts, rtype=rtype, text=text)


def _scoped(ts: str, pr: str) -> ModelLandingLedgerRow:
    return _row(
        ts,
        "RELEASE",
        f"{ts} | RELEASE | lane=orchestrator-31d9 | to=landing-controller | re={HOLD_ID} | pr={pr} | text",
    )


@pytest.mark.unit
def test_scoped_release_keeps_the_repository_hold_in_force() -> None:
    rows = [
        _row("2026-10-26T17:24:04Z", "HOLD", HOLD),
        _scoped("2026-10-30T14:35:39Z", "held_repo#1204"),
    ]
    assert list(fixer_hold_reasons(rows, NOW)) == ["held_repo"]
    assert fixer_hold_released_prs(rows, NOW) == ["OmniNode-ai/held_repo#1204"]


@pytest.mark.unit
def test_unscoped_release_still_ends_the_hold() -> None:
    rows = [
        _row("2026-10-26T17:24:04Z", "HOLD", HOLD),
        _row(
            "2026-10-31T00:00:00Z",
            "RELEASE",
            f"2026-10-31T00:00:00Z | RELEASE | lane=x | re={HOLD_ID} | text",
        ),
    ]
    assert fixer_hold_reasons(rows, NOW) == {}
    assert fixer_hold_released_prs(rows, NOW) == []


@pytest.mark.unit
def test_scoped_release_of_an_ended_hold_names_no_pr() -> None:
    rows = [
        _row("2026-10-26T17:24:04Z", "HOLD", HOLD),
        _scoped("2026-10-30T14:35:39Z", "held_repo#1204"),
    ]
    later = datetime(2026, 11, 10, tzinfo=UTC)
    assert fixer_hold_reasons(rows, later) == {}
    assert fixer_hold_released_prs(rows, later) == []


@pytest.mark.unit
def test_fixer_hold_scoped_release_dispatches_only_the_named_pr() -> None:
    held = Pr(7, annotations={"repo-evidence / dod-verify": "failure of its own"})
    named = Pr(8, annotations={"repo-evidence / dod-verify": "another failure"})
    world = CauseWorld(
        [held, named],
        fixer_hold=[REPO],
        fixer_hold_released_prs=[named.key],
    )
    assert dispatched(world.step()) == [named.key]


@pytest.mark.unit
def test_fixer_hold_with_no_release_dispatches_nothing() -> None:
    held = Pr(7, annotations={"repo-evidence / dod-verify": "failure of its own"})
    world = CauseWorld([held], fixer_hold=[REPO])
    assert dispatched(world.step()) == []

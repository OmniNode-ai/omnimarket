# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ticket whose earlier lane ended already-built or skipped-owned is held for the closer (OMN-22347).

2026-11-09: the lab fill sent OMN-22662 for the fifth time that day, and OMN-22663 and OMN-22658 again
at 13:22Z. Each lane found the fix already on dev (omnimarket#5194, #5210, #5189) and ended skipped. The
fill's lanes are named ``fill-ticket-OMN-<n>-<tag>``, which the replay did not read as attempts, and
skipped outcomes were never a reason to wait, so every tick built the same lanes again.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_selection_compute.handlers import (
    HandlerLabFillSelection,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    EnumLabFillSkipReason,
    ModelLabFillCandidateInput,
    ModelLabFillDeployment,
    ModelLabFillSelectionRequest,
    ModelLabFillSelectionResult,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-09T14:05:00Z"
DEPLOYMENT = ModelLabFillDeployment(("change_control",), ("registry",), ("docs",))
# The ledger rows of the three 13:22Z lanes, as recorded (host and request cells dropped).
TODAY = (
    "2026-11-09T13:28:47Z | CLAIM | lane=fill-ticket-OMN-22658-20261109T1322Z | ticket=OMN-22658 | priority=other",
    "2026-11-09T13:29:48Z | TERMINAL | lane=fill-ticket-OMN-22658-20261109T1322Z | ticket=OMN-22658 | outcome=skipped | worktree=none | band=B3",
    "2026-11-09T13:29:18Z | CLAIM | lane=fill-ticket-OMN-22662-20261109T1322Z | ticket=OMN-22662 | priority=other",
    "2026-11-09T13:29:58Z | TERMINAL | lane=fill-ticket-OMN-22662-20261109T1322Z | ticket=OMN-22662 | outcome=skipped-owned",
    "2026-11-09T13:30:27Z | CLAIM | lane=fill-ticket-OMN-22663-20261109T1322Z | ticket=OMN-22663 | priority=other",
    "2026-11-09T13:31:17Z | TERMINAL | lane=fill-ticket-OMN-22663-20261109T1322Z | ticket=OMN-22663 | outcome=skipped-owned",
)
MERGED = {
    "OMN-22662": "omnimarket#5194",
    "OMN-22663": "omnimarket#5210",
    "OMN-22658": "omnimarket#5189",
}


def _candidate(ticket: str, **over: Any) -> ModelLabFillCandidateInput:
    fields: dict[str, Any] = {
        "key": f"ticket:{ticket}",
        "kind": "ticket",
        "ticket": ticket,
        "ticket_updated_at": "2026-11-09T09:00:00Z",
        "merged_prs": (MERGED[ticket],) if ticket in MERGED else (),
    }
    fields.update(over)
    return ModelLabFillCandidateInput(**fields)


def _select(
    candidates: tuple[ModelLabFillCandidateInput, ...],
    ledger: tuple[str, ...] = TODAY,
    idle_slots: int = 4,
) -> ModelLabFillSelectionResult:
    return HandlerLabFillSelection().handle(
        ModelLabFillSelectionRequest(
            now=NOW,
            candidates=candidates,
            ledger_lines=ledger,
            deployment=DEPLOYMENT,
            idle_slots=idle_slots,
        )
    )


def test_todays_three_tickets_are_held_for_the_closer_not_dispatched() -> None:
    result = _select((*(_candidate(t) for t in MERGED), _candidate("OMN-1")))

    by_ticket = {d.ticket: d for d in result.decisions}
    for ticket, pr in MERGED.items():
        assert by_ticket[ticket].reason is EnumLabFillSkipReason.ALREADY_BUILT
        assert by_ticket[ticket].detail == pr
        assert by_ticket[ticket].caller_reason == "already-built:" + pr
    assert [d.ticket for d in result.closer] == ["OMN-22662", "OMN-22663", "OMN-22658"]
    assert result.dod_verify == ()
    assert by_ticket["OMN-1"].reason is None


def test_an_updated_ticket_is_released_to_the_dod_verify_path() -> None:
    moved = _candidate("OMN-22662", ticket_updated_at="2026-11-09T13:45:00Z")
    result = _select((moved,))

    assert result.decisions[0].reason is EnumLabFillSkipReason.IMPLEMENTATION_MERGED
    assert result.closer == ()
    assert [d.ticket for d in result.dod_verify] == ["OMN-22662"]


def test_skipped_owned_without_a_merged_pr_is_not_already_built() -> None:
    result = _select((_candidate("OMN-22662", merged_prs=()),))

    assert result.decisions[0].reason is None
    assert result.closer == ()


def test_the_terminal_that_names_a_merged_pr_counts_as_its_evidence() -> None:
    ledger = (
        "2026-11-09T12:00:00Z | STATUS | lane=lander | ticket=OMN-22663 | merged=omnimarket#5210",
        "2026-11-09T13:30:27Z | CLAIM | lane=fill-ticket-OMN-22663-20261109T1322Z | ticket=OMN-22663",
        "2026-11-09T13:40:00Z | TERMINAL | lane=fill-ticket-OMN-22663-20261109T1322Z | ticket=OMN-22663 | outcome=already-built | pr=omnimarket#5210",
    )
    result = _select((_candidate("OMN-22663", merged_prs=()),), ledger)

    assert result.decisions[0].reason is EnumLabFillSkipReason.ALREADY_BUILT
    assert result.decisions[0].detail == "omnimarket#5210"


def test_a_blocked_fill_ticket_lane_is_read_as_an_attempt() -> None:
    ledger = (
        "2026-11-09T13:00:00Z | CLAIM | lane=fill-ticket-OMN-5-20261109T1300Z | ticket=OMN-5",
        "2026-11-09T13:10:00Z | TERMINAL | lane=fill-ticket-OMN-5-20261109T1300Z | ticket=OMN-5 | outcome=blocked",
    )
    result = _select((_candidate("OMN-5"),), ledger)

    assert result.decisions[0].reason is EnumLabFillSkipReason.UNCHANGED_INPUT

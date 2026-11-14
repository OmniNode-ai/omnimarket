# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger's TERMINAL rows drive both the selection and the closer's reader; a shape change on either side is caught (OMN-23021, OMN-22112).

The selection (HandlerLabFillSelection) and the closer (built_reports_from_ledger, closer_route) read the same ledger rows. This test pins the seam: the cells outcome=, falsifiers=, the lane name lab-fill-omn<n>-<tag>, and the CLAIM row before each TERMINAL. A misspelt cell (falsifier= instead of falsifiers=) must make both sides refuse the ticket, and a malformed first cell must be ignored by the selection without crashing the closer's reader.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.handlers.rules_lab_fill_closer import (
    built_reports_from_ledger,
    closer_route,
)
from omnimarket.models.lab_fill.model_lab_fill_closer import (
    EnumLabFillCloserReason,
)
from omnimarket.nodes.node_lab_fill_selection_compute.handlers import (
    HandlerLabFillSelection,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    EnumLabFillSkipReason,
    ModelLabFillCandidateInput,
    ModelLabFillDeployment,
    ModelLabFillSelectionRequest,
)

pytestmark = pytest.mark.unit

CLAIM_A = "2026-11-14T00:07:54Z | CLAIM | lane=lab-fill-omn23021-11132350 | ticket=OMN-23021 | actor=script:onex-remote-lane | model=claude-sonnet | parent=orchestrator-bb1a"
TERM_A = "2026-11-14T00:14:37Z | TERMINAL | lane=lab-fill-omn23021-11132350 | ticket=OMN-23021 | actor=claude:sonnet:remote-lane | model=sonnet | friction=none | closes-CLAIM=2026-11-14T00:07:54Z | outcome=skipped-already-built | worktree=none | delegation=na:read-only-verification | falsifiers=pass | owning-repo=omnimarket | evidence=.claude_scratch/reports/lab-fill-omn23021-11132350-2026-11-14T0014Z.md | parent=orchestrator-bb1a"
CLAIM_B = "2026-11-14T00:06:41Z | CLAIM | lane=lab-fill-omn22112-11132350 | ticket=OMN-22112 | actor=script:onex-remote-lane | model=claude-sonnet | parent=orchestrator-bb1a"
TERM_B = "2026-11-14T00:14:54Z | TERMINAL | lane=lab-fill-omn22112-11132350 | ticket=OMN-22112 | actor=claude:sonnet:remote-lane | model=sonnet | friction=none | closes-CLAIM=2026-11-14T00:06:41Z | outcome=skipped-already-built | worktree=none | owning-repo=omnibase_infra | falsifiers=fail | delegated=1 | evidence=.claude_scratch/reports/lab-fill-omn22112-11132350-2026-11-14T0020Z.md | parent=orchestrator-bb1a"
NOW = "2026-11-14T00:40:00Z"

DEPLOYMENT = ModelLabFillDeployment(
    ("change_control",),
    ("registry",),
    ("docs",),
    terminal_hold_outcomes=("skipped-already-built",),
    terminal_hold_window_hours=24,
)

ROWS = (CLAIM_A, TERM_A, CLAIM_B, TERM_B)


def _candidate(ticket: str, **over: Any) -> ModelLabFillCandidateInput:
    fields: dict[str, Any] = {
        "key": f"ticket:{ticket}",
        "kind": "ticket",
        "ticket": ticket,
        "ticket_updated_at": "2026-11-13T20:00:00Z",
    }
    fields.update(over)
    return ModelLabFillCandidateInput(**fields)


def _select(
    candidates: tuple[ModelLabFillCandidateInput, ...],
    ledger: tuple[str, ...] = ROWS,
) -> Any:
    return HandlerLabFillSelection().handle(
        ModelLabFillSelectionRequest(
            now=NOW,
            candidates=candidates,
            ledger_lines=ledger,
            deployment=DEPLOYMENT,
            idle_slots=4,
        )
    )


def test_omn23021_selection_and_closer_agree_already_built() -> None:
    result = _select((_candidate("OMN-23021"),))
    assert result.decisions[0].reason is EnumLabFillSkipReason.ALREADY_BUILT
    assert [d.ticket for d in result.closer] == ["OMN-23021"]

    reports = built_reports_from_ledger(list(ROWS))
    report = next(r for r in reports if r.ticket == "OMN-23021")
    assert report.falsifiers_passed is True
    route = closer_route("OMN-23021", "ticket", built=reports)
    assert route is not None
    assert route.reason is EnumLabFillCloserReason.ALREADY_BUILT


def test_omn22112_selection_and_closer_agree_readback() -> None:
    result = _select((_candidate("OMN-22112"),))
    assert result.decisions[0].reason is EnumLabFillSkipReason.READBACK
    assert [d.ticket for d in result.readback] == ["OMN-22112"]
    assert [d.ticket for d in result.closer] == []

    reports = built_reports_from_ledger(list(ROWS))
    report = next(r for r in reports if r.ticket == "OMN-22112")
    assert report.falsifiers_passed is False
    assert closer_route("OMN-22112", "ticket", built=reports) is None


def test_failing_control_a_misspelt_falsifiers_cell_is_refused_by_both_sides() -> None:
    bad = TERM_A.replace("falsifiers=", "falsifier=")
    ledger = (CLAIM_A, bad, CLAIM_B, TERM_B)
    result = _select((_candidate("OMN-23021"),), ledger=ledger)
    assert result.decisions[0].reason is EnumLabFillSkipReason.READBACK
    assert [d.ticket for d in result.closer] == []

    reports = built_reports_from_ledger(list(ledger))
    report = next(r for r in reports if r.ticket == "OMN-23021")
    assert report.falsifiers_passed is False
    assert closer_route("OMN-23021", "ticket", built=reports) is None


def test_error_chain_a_malformed_first_cell_is_ignored_by_the_selection_and_does_not_crash_the_closer() -> (
    None
):
    bad = "not-a-timestamp | TERMINAL | lane=lab-fill-omn23021-11132350 | ticket=OMN-23021 | outcome=skipped-already-built | falsifiers=pass"
    ledger = (CLAIM_A, bad, CLAIM_B, TERM_B)
    # The selection drops the unreadable row, so the lane stays open (OWNED); the closer's reader does not
    # read the stamp, so it still takes the row as a pass. The two sides differ on this row; this pins it.
    result = _select((_candidate("OMN-23021"),), ledger=ledger)
    assert result.decisions[0].reason is EnumLabFillSkipReason.OWNED
    assert [d.ticket for d in result.closer] == []

    reports = built_reports_from_ledger(list(ledger))
    report = next(r for r in reports if r.ticket == "OMN-23021")
    assert report.falsifiers_passed is True
    route = closer_route("OMN-23021", "ticket", built=reports)
    assert route is not None
    assert route.reason is EnumLabFillCloserReason.ALREADY_BUILT

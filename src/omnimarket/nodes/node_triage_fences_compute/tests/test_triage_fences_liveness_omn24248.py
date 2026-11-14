# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ledger CLAIM fences its tickets only while its lane is live (OMN-24248).

On 2026-11-13 at 15:31Z the fence list kept every CLAIM whose lane had written any ledger row in
the last 48 hours: 54 tickets fenced, 16 with a live lane, 42 lab places idle with dispatched=0. A
lane is live while its runner receipt reads running or while it wrote a ledger row inside the
window (45 minutes in the overlay: the window queue-status and the landing controller use).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_triage_fences_compute.handlers.handler_triage_fences_derive import (
    HandlerTriageFencesDerive,
)
from omnimarket.nodes.node_triage_fences_compute.models import (
    ModelTriageFencesDeriveRequest,
    ModelTriageFencesVerdict,
)

NOW = datetime(2026, 11, 13, 15, 0, 0, tzinfo=UTC)
WINDOW_HOURS = 0.75  # the overlay's 45 minutes

CLAIM = "2026-11-13T10:00:00Z | CLAIM | lane=lane-a | ticket=OMN-90001 | actor=script:t | model=m | x"
TERMINAL = "2026-11-13T12:00:00Z | TERMINAL | lane=lane-a | ticket=OMN-90001 | friction=none | done"
RECENT_ROW = "2026-11-13T14:50:00Z | STATUS | lane=lane-a | still working"


def _derive(
    *rows: str, stale_after_hours: float = WINDOW_HOURS, running: tuple[str, ...] = ()
) -> ModelTriageFencesVerdict:
    return HandlerTriageFencesDerive().handle(
        ModelTriageFencesDeriveRequest(
            ledger_name="LEDGER.md",
            ledger_text="\n".join(rows) + "\n",
            now=NOW,
            stale_after_hours=stale_after_hours,
            pr_window_hours=6.0,
            running_lanes=running,
        )
    )


def test_dead_claim_does_not_fence() -> None:
    verdict = _derive(CLAIM)
    assert verdict.fenced_tickets == ()
    assert verdict.counts.stale_claims == 1
    assert verdict.counts.fenced_tickets == 0
    assert [c.tickets for c in verdict.stale_claims] == [("OMN-90001",)]
    assert verdict.stale_claims[0].live_basis == ""


def test_old_window_fences_the_dead_claim_control() -> None:
    """The failing control: the 48 hour window of 2026-11-13 fences the same dead claim."""
    verdict = _derive(CLAIM, stale_after_hours=48.0)
    assert verdict.fenced_tickets == ("OMN-90001",)
    assert verdict.counts.stale_claims == 0


def test_live_claim_still_fences_on_recent_row() -> None:
    verdict = _derive(CLAIM, RECENT_ROW)
    assert verdict.fenced_tickets == ("OMN-90001",)
    assert [c.live_basis for c in verdict.open_claims] == ["recent-row"]


def test_live_claim_still_fences_on_running_receipt() -> None:
    verdict = _derive(CLAIM, running=("LANE-A",))  # the case differs on purpose
    assert verdict.fenced_tickets == ("OMN-90001",)
    assert [c.live_basis for c in verdict.open_claims] == ["running-receipt"]
    assert verdict.counts.stale_claims == 0


def test_a_running_receipt_of_another_lane_keeps_nothing() -> None:
    verdict = _derive(CLAIM, running=("lane-b",))
    assert verdict.fenced_tickets == ()
    assert verdict.counts.stale_claims == 1


def test_a_closed_claim_never_fences_even_when_its_lane_is_running() -> None:
    verdict = _derive(CLAIM, TERMINAL, running=("lane-a",))
    assert verdict.fenced_tickets == ()
    assert verdict.counts.open_claims == 0
    assert verdict.counts.stale_claims == 0


def test_same_input_gives_a_byte_identical_verdict() -> None:
    first = _derive(CLAIM, RECENT_ROW, running=("lane-a",))
    second = _derive(CLAIM, RECENT_ROW, running=("lane-a",))
    assert first.model_dump_json() == second.model_dump_json()


def test_golden_chain_three_claims() -> None:
    """One claim live by a row, one live by a running receipt, one dead."""
    verdict = _derive(
        "2026-11-13T09:00:00Z | CLAIM | lane=by-row | ticket=OMN-90010 | actor=script:t | x",
        "2026-11-13T14:40:00Z | STATUS | lane=by-row | working",
        "2026-11-13T09:05:00Z | CLAIM | lane=by-receipt | ticket=OMN-90011 | actor=script:t | x",
        "2026-11-13T09:10:00Z | CLAIM | lane=dead | ticket=OMN-90012 | actor=script:t | x",
        running=("by-receipt",),
    )
    assert verdict.fenced_tickets == ("OMN-90010", "OMN-90011")
    assert {c.lane: c.live_basis for c in verdict.open_claims} == {
        "by-row": "recent-row",
        "by-receipt": "running-receipt",
    }
    assert [(c.lane, c.live_basis) for c in verdict.stale_claims] == [("dead", "")]
    assert verdict.counts.open_claims == 2
    assert verdict.counts.stale_claims == 1
    assert verdict.counts.fenced_tickets == 2
    assert verdict.counts.open_pr_fences == 0


def test_error_chain_request_refusals() -> None:
    base = {"ledger_name": "L", "ledger_text": "", "now": NOW, "pr_window_hours": 6.0}
    with pytest.raises(ValidationError):
        ModelTriageFencesDeriveRequest.model_validate({**base, "stale_after_hours": 0})
    with pytest.raises(ValidationError):
        ModelTriageFencesDeriveRequest.model_validate(
            {k: v for k, v in base.items() if k != "pr_window_hours"}
            | {"stale_after_hours": 1}
        )
    with pytest.raises(ValidationError):
        ModelTriageFencesDeriveRequest.model_validate(
            {**base, "stale_after_hours": 1, "path": "/somewhere"}
        )
    with pytest.raises(ValidationError):
        ModelTriageFencesDeriveRequest.model_validate(
            {**base, "stale_after_hours": 1, "now": datetime(2026, 11, 13, 15, 0)}
        )

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane's own TERMINAL closes the child-ticket CLAIMs the lane runner wrote for it (OMN-24248).

The lane runner writes a second CLAIM for a lane whose brief names a child ticket, carrying
``parent_ticket=<carrier ticket>``. The lane's TERMINAL names the carrier ticket only, so no ticket
intersection ever paired the child claim with it: on 2026-11-13, 37 of 39 dead claims were such
children, and the TERMINAL row itself kept each lane "live" for the next window.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_triage_fences_compute.handlers.handler_triage_fences_derive import (
    HandlerTriageFencesDerive,
)
from omnimarket.nodes.node_triage_fences_compute.models import (
    ModelTriageFencesDeriveRequest,
    ModelTriageFencesVerdict,
)

FIXTURES = (
    Path(__file__).parents[2] / "node_triage_fences_effect" / "tests" / "fixtures"
)
CARRIER_CLAIM = (
    "2026-11-13T10:24:13Z | CLAIM | lane=closer-fix-6764 | ticket=OMN-19998 | "
    "actor=script:onex-remote-lane | model=claude-sonnet | worktree=none | carrier claim"
)


def _derive(
    text: str, now: str, *, running: tuple[str, ...] = ()
) -> ModelTriageFencesVerdict:
    return HandlerTriageFencesDerive().handle(
        ModelTriageFencesDeriveRequest(
            ledger_name="seam.md",
            ledger_text=text,
            now=datetime.fromisoformat(now).astimezone(UTC),
            stale_after_hours=0.75,
            pr_window_hours=6.0,
            running_lanes=running,
        )
    )


def _real_rows() -> str:
    return (FIXTURES / "ledger_seam_rows.md").read_text(encoding="utf-8")


def test_the_lanes_own_terminal_closes_its_child_ticket_claim() -> None:
    """The real rows of lane closer-fix-6764: carrier CLAIM, child CLAIM, carrier TERMINAL."""
    verdict = _derive(CARRIER_CLAIM + "\n" + _real_rows(), "2026-11-13T12:00:00+00:00")
    assert verdict.fenced_tickets == ()
    assert verdict.counts.open_claims == 0


def test_control_without_the_carrier_claim_the_child_claim_is_closed_all_the_same() -> (
    None
):
    """The carrier CLAIM rolled into an archive; the lane's TERMINAL still ends its child claim."""
    verdict = _derive(_real_rows(), "2026-11-13T12:00:00+00:00")
    assert verdict.fenced_tickets == ()


def test_a_child_claim_of_another_lane_is_not_closed() -> None:
    other = _real_rows().replace("lane=closer-fix-6764", "lane=other-lane").splitlines()
    mixed = "\n".join(
        [
            other[0],  # the child claim of other-lane
            _real_rows().splitlines()[
                1
            ],  # closer-fix-6764's TERMINAL naming the carrier
        ]
    )
    verdict = _derive(mixed, "2026-11-13T12:00:00+00:00", running=("other-lane",))
    assert verdict.fenced_tickets == ("OMN-24138",)


def test_a_child_claim_written_after_the_terminal_stays_open() -> None:
    """A reused lane handle starting new work is not closed by an older TERMINAL."""
    rows = _real_rows().splitlines()
    later_child = rows[0].replace("2026-11-13T10:45:48Z", "2026-11-13T12:10:00Z")
    verdict = _derive(
        "\n".join([rows[1], later_child]),
        "2026-11-13T12:20:00+00:00",
        running=("closer-fix-6764",),
    )
    assert verdict.fenced_tickets == ("OMN-24138",)


def test_a_terminal_naming_another_carrier_closes_no_child() -> None:
    rows = _real_rows().splitlines()
    unrelated = rows[1].replace("ticket=OMN-19998", "ticket=OMN-90001")
    verdict = _derive(
        "\n".join([rows[0], unrelated]),
        "2026-11-13T12:00:00+00:00",
        running=("closer-fix-6764",),
    )
    assert verdict.fenced_tickets == ("OMN-24138",)

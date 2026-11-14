# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The AUTHORITY line cites the ruling's own ticket, not the run's parent ticket (OMN-17427).

A smoke brief cited a ruling with ticket OMN-22478 where the ledger RULING row carries OMN-17427. The ticket of a ruling is
the ``ticket=`` cell of its own ledger RULING row; the brief must cite that, and only fall back to the parent ticket when no
ruling ticket is supplied.
"""

from __future__ import annotations

import pytest

from omnimarket.handlers.rules_lab_fill_ruling import ruling_row_ticket
from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_lane_render import (
    HandlerLabFillLaneRender,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models import (
    ModelLabFillDispatchItem,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_lane_render import (
    ModelLabFillLaneRenderRequest,
    ModelLabFillRenderConfig,
)

pytestmark = pytest.mark.unit

_ROWS = [
    '2026-11-09T04:08:23Z | RULING | lane=orchestrator-31d9 | principal=x | ticket=OMN-17427 | question=q | kind=decision | "words"'
]


def _brief(*, authority_ticket: str = "") -> str:
    item = ModelLabFillDispatchItem(
        lane="lab-fill-ticket",
        kind="ticket",
        id="row-1",
        ticket="OMN-9",
        pr="",
        repo="repo_a",
        ref="main",
        title="the ticket title",
        updated_at="2026-11-07T00:00:00Z",
        host="h1",
        pinned=False,
        engine="sonnet",
        route="ROUTE: band=B3 score=10",
    )
    config = ModelLabFillRenderConfig(
        pillar="landing",
        parent_lane="lab-fill",
        parent_ticket="OMN-22478",
        run_key="2026-11-07",
        authority_ruling="2026-11-09T04:08:23Z",
        authority_lane="orchestrator-31d9",
        authority_ticket=authority_ticket,
    )
    return (
        HandlerLabFillLaneRender()
        .handle(ModelLabFillLaneRenderRequest(item=item, config=config))
        .brief
    )


def test_ruling_row_ticket_reads_the_ruling_rows_ticket() -> None:
    assert (
        ruling_row_ticket(_ROWS, "2026-11-09T04:08:23Z", "orchestrator-31d9")
        == "OMN-17427"
    )


def test_authority_line_cites_the_ruling_ticket_not_the_parent() -> None:
    brief = _brief(authority_ticket="OMN-17427")
    assert "ticket=OMN-17427" in brief
    assert "ticket=OMN-22478" not in brief


def test_authority_line_falls_back_to_the_parent_ticket() -> None:
    # The old line cited the parent ticket; with no ruling ticket supplied it still does.
    assert "ticket=OMN-22478" in _brief()


def test_ruling_row_ticket_refuses_a_missing_ruling() -> None:
    with pytest.raises(ValueError, match="2026-11-09T04:08:23Z"):
        ruling_row_ticket(
            [
                "2026-11-09T04:08:23Z | CLAIM | lane=orchestrator-31d9 | ticket=OMN-17427"
            ],
            "2026-11-09T04:08:23Z",
            "orchestrator-31d9",
        )


def test_ruling_row_ticket_refuses_conflicting_tickets() -> None:
    rows = [
        "2026-11-09T04:08:23Z | RULING | lane=orchestrator-31d9 | ticket=OMN-17427",
        "2026-11-09T04:08:23Z | RULING | lane=orchestrator-31d9 | ticket=OMN-22478",
    ]
    with pytest.raises(ValueError, match="conflicting"):
        ruling_row_ticket(rows, "2026-11-09T04:08:23Z", "orchestrator-31d9")


def test_ruling_row_ticket_refuses_a_row_without_a_ticket_cell() -> None:
    with pytest.raises(ValueError, match="no ticket cell"):
        ruling_row_ticket(
            ["2026-11-09T04:08:23Z | RULING | lane=orchestrator-31d9 | question=q"],
            "2026-11-09T04:08:23Z",
            "orchestrator-31d9",
        )


def test_authority_ticket_must_carry_the_omn_prefix() -> None:
    with pytest.raises(ValueError, match="authority_ticket"):
        ModelLabFillRenderConfig(
            pillar="landing",
            parent_lane="lab-fill",
            parent_ticket="OMN-22478",
            run_key="2026-11-07",
            authority_ruling="2026-11-09T04:08:23Z",
            authority_lane="orchestrator-31d9",
            authority_ticket="17427",
        )


def test_two_rulings_resolve_to_their_own_tickets() -> None:
    rows = [
        "2026-11-09T04:08:23Z | RULING | lane=orchestrator-31d9 | ticket=OMN-17427",
        "2026-11-09T05:00:00Z | RULING | lane=orchestrator-31d9 | ticket=OMN-22478",
    ]
    assert (
        ruling_row_ticket(rows, "2026-11-09T04:08:23Z", "orchestrator-31d9")
        == "OMN-17427"
    )
    assert (
        ruling_row_ticket(rows, "2026-11-09T05:00:00Z", "orchestrator-31d9")
        == "OMN-22478"
    )

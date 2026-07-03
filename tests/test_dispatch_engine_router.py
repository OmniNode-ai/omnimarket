# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the dispatch_engine router (OMN-13834).

Proves the node is a REAL thin router — not a placeholder — by routing a fixture
candidate ticket set through RSD scoring (node_rsd_fill_compute) then self-healing
per-repo fan-out (node_self_healing_dispatch_orchestrator) and asserting the
resulting dispatch receipt carries concrete worker specs (repo + ticket_ids +
deterministic worker names), ranked candidates, and honest cut accounting.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from omnimarket.events.self_healing_dispatch import ModelDispatchGroup
from omnimarket.models.model_scored_ticket import ModelScoredTicket
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.handlers.handler_dispatch_router import (
    HandlerDispatchEngineRouter,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.models import (
    EnumDispatchEngineStatus,
    ModelDispatchEngineRequest,
)

_CID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


def _fixture_tickets() -> tuple[ModelScoredTicket, ...]:
    """A representative sprint slice with distinct scores, priorities, repos."""
    return (
        ModelScoredTicket(
            ticket_id="OMN-100", title="urgent", rsd_score=0.92, priority=1
        ),
        ModelScoredTicket(ticket_id="OMN-101", title="low", rsd_score=0.18, priority=4),
        ModelScoredTicket(ticket_id="OMN-102", title="mid", rsd_score=0.61, priority=2),
        ModelScoredTicket(
            ticket_id="OMN-103", title="high", rsd_score=0.74, priority=2
        ),
    )


_REPO_HINTS = {
    "OMN-100": "omniclaude",
    "OMN-102": "omnibase_core",
    "OMN-103": "omniclaude",
}


@pytest.mark.unit
async def test_router_produces_real_worker_specs_on_fixture_set() -> None:
    """A routed dispatch yields ranked candidates + concrete per-repo worker specs."""
    request = ModelDispatchEngineRequest(
        correlation_id=_CID,
        candidate_tickets=_fixture_tickets(),
        repo_hints=_REPO_HINTS,
        top_n=3,
    )

    receipt = await HandlerDispatchEngineRouter().route(request)

    # Real receipt, not a placeholder string.
    assert receipt.status is EnumDispatchEngineStatus.PLANNED
    assert receipt.run_id == f"dispatch-engine-{_CID}"
    assert receipt.total_candidates == 4
    assert receipt.total_selected == 3

    # RSD ranking: top_n=3 keeps the three highest scores in descending order.
    assert [t.ticket_id for t in receipt.scored_candidates] == [
        "OMN-100",
        "OMN-103",
        "OMN-102",
    ]

    # Fan-out grouped survivors per repo into concrete worker specs.
    specs_by_repo = {s.repo: s for s in receipt.worker_specs}
    assert set(specs_by_repo) == {"omniclaude", "omnibase_core"}
    assert specs_by_repo["omniclaude"].ticket_ids == ("OMN-100", "OMN-103")
    assert specs_by_repo["omnibase_core"].ticket_ids == ("OMN-102",)

    # Worker names are deterministic and NOT placeholder / dry-run strings.
    for spec in receipt.worker_specs:
        assert spec.worker_name == f"{receipt.run_id}-{spec.repo}"
        assert not spec.worker_name.startswith("dry-run-")


@pytest.mark.unit
async def test_router_min_score_cut_drops_low_candidates() -> None:
    """min_score filters weak candidates before fan-out."""
    request = ModelDispatchEngineRequest(
        correlation_id=_CID,
        candidate_tickets=_fixture_tickets(),
        repo_hints=_REPO_HINTS,
        top_n=5,
        min_score=0.7,
    )

    receipt = await HandlerDispatchEngineRouter().route(request)

    assert receipt.total_selected == 2
    assert {t.ticket_id for t in receipt.scored_candidates} == {"OMN-100", "OMN-103"}
    # Both survivors map to omniclaude → a single worker spec.
    assert len(receipt.worker_specs) == 1
    assert receipt.worker_specs[0].repo == "omniclaude"
    assert receipt.worker_specs[0].ticket_ids == ("OMN-100", "OMN-103")


@pytest.mark.unit
async def test_router_no_candidates_when_all_below_min_score() -> None:
    """When nothing survives the cut the receipt is an honest empty cycle."""
    request = ModelDispatchEngineRequest(
        correlation_id=_CID,
        candidate_tickets=_fixture_tickets(),
        top_n=5,
        min_score=0.99,
    )

    receipt = await HandlerDispatchEngineRouter().route(request)

    assert receipt.status is EnumDispatchEngineStatus.NO_CANDIDATES
    assert receipt.scored_candidates == ()
    assert receipt.worker_specs == ()
    assert receipt.total_candidates == 4
    assert receipt.total_selected == 0


@pytest.mark.unit
async def test_router_empty_input_no_candidates() -> None:
    """No candidate tickets → no_candidates (the bare skill-shim path)."""
    receipt = await HandlerDispatchEngineRouter().route(
        ModelDispatchEngineRequest(correlation_id=_CID)
    )
    assert receipt.status is EnumDispatchEngineStatus.NO_CANDIDATES
    assert receipt.total_candidates == 0
    assert receipt.worker_specs == ()


@pytest.mark.unit
async def test_router_dry_run_reports_dry_run_status() -> None:
    """dry_run still groups (worker specs present) but reports DRY_RUN status."""
    request = ModelDispatchEngineRequest(
        correlation_id=_CID,
        candidate_tickets=_fixture_tickets(),
        repo_hints=_REPO_HINTS,
        top_n=3,
        dry_run=True,
    )

    receipt = await HandlerDispatchEngineRouter().route(request)

    assert receipt.status is EnumDispatchEngineStatus.DRY_RUN
    assert receipt.dry_run is True
    assert receipt.total_selected == 3
    assert receipt.worker_specs  # grouping still produced a plan


class _RecordingDispatcher:
    """Deterministic in-process dispatcher that records launched groups."""

    def __init__(self) -> None:
        self.launched: list[tuple[str, tuple[str, ...]]] = []

    def dispatch_group(self, group: ModelDispatchGroup, *, run_id: str) -> str:
        self.launched.append((group.repo, group.ticket_ids))
        return f"worker-{run_id}-{group.repo}"


@pytest.mark.unit
async def test_router_live_dispatcher_reports_dispatched() -> None:
    """With a live dispatcher injected, the router launches and reports DISPATCHED."""
    dispatcher = _RecordingDispatcher()
    request = ModelDispatchEngineRequest(
        correlation_id=_CID,
        candidate_tickets=_fixture_tickets(),
        repo_hints=_REPO_HINTS,
        top_n=3,
    )

    receipt = await HandlerDispatchEngineRouter(dispatcher=dispatcher).route(request)

    assert receipt.status is EnumDispatchEngineStatus.DISPATCHED
    # The live dispatcher was actually invoked for each grouped repo.
    assert {repo for repo, _ in dispatcher.launched} == {"omniclaude", "omnibase_core"}
    assert receipt.worker_specs

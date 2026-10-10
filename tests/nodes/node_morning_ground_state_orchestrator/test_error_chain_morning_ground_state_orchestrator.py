# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: bad delegation evidence stops dependent work; best-effort legs fail open."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import JsonValue

from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_ground_state import (
    HandlerMorningGroundState,
    delegation_problem,
)

from .helpers import (
    PHASES,
    FixtureGateway,
    PhaseEvidenceGateway,
    public_overlay,
    request,
)


def _handler(gateway: FixtureGateway) -> HandlerMorningGroundState:
    return HandlerMorningGroundState(None, gateway, public_overlay())


@pytest.mark.parametrize(
    ("cell", "problem"),
    [
        (None, "missing or invalid cell"),
        ({"delegated": 0, "runs": [], "reason": ""}, "zero delegation reason refused"),
        ({"delegated": 1, "runs": [], "reason": ""}, "delegated steps have no runs"),
        ({"delegated": 1, "runs": [" "], "reason": ""}, "invalid run ids"),
        ({"delegated": 1, "runs": ["\t\n"], "reason": ""}, "invalid run ids"),
        ({"delegated": 1, "runs": ["stub run"], "reason": ""}, "invalid run ids"),
        ({"delegated": 1, "runs": [" stub-run"], "reason": ""}, "invalid run ids"),
        ({"delegated": 1, "runs": ["stub-run\n"], "reason": ""}, "invalid run ids"),
        ({"delegated": 1, "runs": ["stub-run", " "], "reason": ""}, "invalid run ids"),
        (
            {"delegated": 0, "runs": ["stub-run"], "reason": "route-refused:503"},
            "zero delegated count requires empty runs",
        ),
        (
            {
                "delegated": 0,
                "runs": ["stub-run"],
                "reason": "route-unavailable:stub-run",
            },
            "zero delegated count requires empty runs",
        ),
        ({"delegated": 1, "runs": ["run-1"], "reason": ""}, None),
        (
            {"delegated": 0, "runs": [], "reason": "route-refused:unavailable"},
            None,
        ),
        (
            {"delegated": True, "runs": ["run-1"], "reason": ""},
            "invalid delegated count",
        ),
    ],
)
def test_delegation_cells(cell: JsonValue, problem: str | None) -> None:
    assert delegation_problem(cell) == problem


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize(
    "cell",
    [
        None,
        {"delegated": 1, "runs": [], "reason": ""},
        {"delegated": 0, "runs": [], "reason": ""},
    ],
)
def test_invalid_phase_evidence_stops_dependent_work(
    phase: str, cell: JsonValue
) -> None:
    gateway = PhaseEvidenceGateway(phase, cell)
    with pytest.raises(ValueError, match="delegation cell refused"):
        asyncio.run(_handler(gateway).handle(request({"force": True})))
    # GroundState and Triage are siblings; neither may release the dependent phases.
    dependent = PHASES[2:] if phase in PHASES[:2] else PHASES[PHASES.index(phase) + 1 :]
    assert not set(dependent).intersection(call["phase"] for call in gateway.calls)


@pytest.mark.parametrize(
    "cell",
    [
        {"delegated": 1, "runs": ["run-1"], "reason": ""},
        {"delegated": 0, "runs": [], "reason": "route-refused:timeout"},
        {"delegated": 0, "runs": [], "reason": "route-unavailable:run-1"},
    ],
)
def test_recorded_phase_evidence_allows_dependent_work(cell: JsonValue) -> None:
    gateway = PhaseEvidenceGateway("GroundState", cell)
    result = asyncio.run(_handler(gateway).handle(request({"force": True})))
    assert result.ground is not None
    assert result.ground["delegation"] == cell
    assert gateway.calls[-1]["phase"] == "Goal"


def test_reconciler_failure_is_best_effort() -> None:
    gateway = FixtureGateway()
    gateway.fail_reconcile = True
    result = asyncio.run(_handler(gateway).handle(request()))
    assert result.phases_run == ["GroundState", "Triage", "Reconcile", "Integrate"]
    assert result.model_dump(mode="json")["ledger_reconcile_failure"] == {
        "failure_type": "RuntimeError",
        "failure_reason": "unavailable reconciler",
    }


def test_dead_precheck_falls_through_to_full_run() -> None:
    gateway = FixtureGateway()
    gateway.fail_precheck = True
    result = asyncio.run(_handler(gateway).handle(request()))
    assert result.expensive_agents_spawned == 4
    assert result.unconditional_agents_spawned == 2
    assert result.precheck == {
        "bypassed": "unavailable - fell through to a full run",
        "failure_type": "TimeoutError",
        "failure_reason": "dead precheck agent",
    }


def test_a_missing_prompt_field_is_refused_not_rendered_blank() -> None:
    overlay = public_overlay()
    broken = overlay.model_copy(
        update={"templates": {**overlay.templates, "ground-state": "uses @@nope@@"}}
    )
    with pytest.raises(ValueError, match="unknown morning prompt field: nope"):
        asyncio.run(
            HandlerMorningGroundState(None, FixtureGateway(), broken).handle(
                request({"force": True})
            )
        )

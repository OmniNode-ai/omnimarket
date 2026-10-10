# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: bad evidence stops the sweep; the precheck fails open."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import JsonValue

from omnimarket.nodes.node_morning_friction_sweep_orchestrator.handlers.handler_morning_friction_sweep import (
    HandlerMorningFrictionSweep,
    delegation_problem,
    premise_audit_problem,
)
from omnimarket.nodes.node_morning_friction_sweep_orchestrator.models import (
    ModelFrictionPhaseRequest,
    ModelMorningFrictionSweepRequest,
)

from .helpers import FixtureGateway, PhaseEvidenceGateway, public_overlay, request

LANES = ["control-lane-a", "control-lane-b", "control-lane-c"]
DELEGATED = ["friction-synthesize", "friction-adjudicate", "friction-report"]


def _handler(gateway: FixtureGateway) -> HandlerMorningFrictionSweep:
    return HandlerMorningFrictionSweep(None, gateway, public_overlay())


@pytest.mark.parametrize(
    ("cell", "problem"),
    [
        (None, "missing or invalid cell"),
        ({"delegated": 0, "runs": [], "reason": ""}, "zero delegation reason refused"),
        ({"delegated": 1, "runs": [], "reason": ""}, "delegated steps have no runs"),
        ({"delegated": 1, "runs": [" "], "reason": ""}, "invalid run ids"),
        ({"delegated": 1, "runs": ["stub run"], "reason": ""}, "invalid run ids"),
        (
            {"delegated": 1, "runs": ["run-1"], "reason": "x"},
            "delegated steps require an empty reason",
        ),
        (
            {"delegated": 0, "runs": ["stub-run"], "reason": "route-refused:503"},
            "zero delegated count requires empty runs",
        ),
        ({"delegated": 1, "runs": ["run-1"], "reason": ""}, None),
        ({"delegated": 0, "runs": [], "reason": "route-refused:unavailable"}, None),
        ({"delegated": 0, "runs": [], "reason": "route-unavailable:run-1"}, None),
        (
            {"delegated": True, "runs": ["run-1"], "reason": ""},
            "invalid delegated count",
        ),
    ],
)
def test_delegation_cells(cell: JsonValue, problem: str | None) -> None:
    assert delegation_problem(cell) == problem


@pytest.mark.parametrize("label", DELEGATED)
@pytest.mark.parametrize(
    "cell",
    [
        None,
        {"delegated": 1, "runs": [], "reason": ""},
        {"delegated": 0, "runs": [], "reason": ""},
    ],
)
def test_invalid_delegation_evidence_fails_the_sweep(
    label: str, cell: JsonValue
) -> None:
    gateway = PhaseEvidenceGateway(label, cell)
    with pytest.raises(ValueError, match="delegation cell refused"):
        asyncio.run(_handler(gateway).handle(request({"force": True})))


def test_an_absent_phase_result_stops_dependent_work() -> None:
    class Absent(FixtureGateway):
        async def run(
            self,
            run: ModelMorningFrictionSweepRequest,
            phase: ModelFrictionPhaseRequest,
        ) -> dict[str, JsonValue] | None:
            result = await super().run(run, phase)
            return None if phase.label == "friction-synthesize" else result

    gateway = Absent()
    with pytest.raises(ValueError, match="absent phase result"):
        asyncio.run(_handler(gateway).handle(request({"force": True})))
    assert "friction-adjudicate" not in [c["label"] for c in gateway.calls]


@pytest.mark.parametrize(
    "cell",
    [
        {"delegated": 1, "runs": ["run-1"], "reason": ""},
        {"delegated": 0, "runs": [], "reason": "route-refused:timeout"},
    ],
)
def test_recorded_delegation_evidence_completes_the_sweep(cell: JsonValue) -> None:
    gateway = PhaseEvidenceGateway("friction-synthesize", cell)
    result = asyncio.run(_handler(gateway).handle(request({"force": True})))
    assert result.synthesis is not None
    assert result.synthesis.delegation.model_dump() == cell


def _measured(**change: JsonValue) -> dict[str, JsonValue]:
    controls: list[JsonValue] = [
        {"lane": lane, "terminal_rows": 2, "evidence": "row"} for lane in LANES
    ]
    cell: dict[str, JsonValue] = {
        "status": "MEASURED",
        "rule_landed_at": "2026-09-15T10:00:00Z",
        "rule_evidence": "ledger row",
        "eligible_terminal_rows": 10,
        "falsified_terminal_rows": 1,
        "positive_controls": controls,
        "unreadable_sources": [],
        "reason": "",
    }
    cell.update(change)
    return cell


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({}, None),
        ({"eligible_terminal_rows": 0}, "measurement lacks complete nonempty coverage"),
        (
            {"unreadable_sources": ["ledger archive"]},
            "measurement lacks complete nonempty coverage",
        ),
        ({"falsified_terminal_rows": 11}, "invalid falsified count"),
        ({"rule_landed_at": "2026-09-15"}, "rule landing lacks timestamp or citation"),
        ({"rule_evidence": " "}, "rule landing lacks timestamp or citation"),
        ({"positive_controls": []}, "all cited positive controls are required"),
        (
            {
                "positive_controls": [
                    {"lane": "other", "terminal_rows": 1, "evidence": "e"}
                ]
            },
            "invalid positive controls",
        ),
        ({"status": "GUESS"}, "invalid audit status"),
        (
            {
                "status": "UNKNOWN",
                "falsified_terminal_rows": None,
                "reason": "no ledger",
            },
            None,
        ),
        (
            {"status": "UNKNOWN", "falsified_terminal_rows": 3, "reason": "no ledger"},
            "UNKNOWN must explain missing evidence and cannot report a count",
        ),
    ],
)
def test_premise_audit_cells(change: dict[str, JsonValue], problem: str | None) -> None:
    assert premise_audit_problem(_measured(**change), LANES) == problem


def test_a_missing_premise_audit_is_refused() -> None:
    assert premise_audit_problem(None, LANES) == "missing or invalid audit"


def test_a_refused_premise_audit_fails_the_sweep() -> None:
    class Unmeasured(FixtureGateway):
        async def run(
            self,
            run: ModelMorningFrictionSweepRequest,
            phase: ModelFrictionPhaseRequest,
        ) -> dict[str, JsonValue] | None:
            result = await super().run(run, phase)
            if phase.label == "friction-report" and result is not None:
                result["premise_audit"] = _measured(eligible_terminal_rows=0)
            return result

    with pytest.raises(ValueError, match="premise audit refused"):
        asyncio.run(_handler(Unmeasured()).handle(request({"force": True})))


def test_dead_precheck_falls_through_to_full_run() -> None:
    gateway = FixtureGateway()
    gateway.fail_precheck = True
    result = asyncio.run(_handler(gateway).handle(request()))
    assert result.short_circuited is None
    assert result.agents == 9
    assert result.precheck.model_dump(mode="json") == {
        "bypassed": "unavailable — fell through to a full run",
        "failure_type": "TimeoutError",
        "failure_reason": "dead precheck agent",
    }


def test_a_precheck_off_its_schema_falls_through_to_full_run() -> None:
    class Garbled(FixtureGateway):
        async def run(
            self,
            run: ModelMorningFrictionSweepRequest,
            phase: ModelFrictionPhaseRequest,
        ) -> dict[str, JsonValue] | None:
            result = await super().run(run, phase)
            if phase.label == "friction-precheck" and result is not None:
                result["verdict"] = "maybe"
            return result

    result = asyncio.run(_handler(Garbled()).handle(request()))
    assert result.short_circuited is None
    assert result.precheck_agents == 1


def test_a_missing_prompt_field_is_refused_not_rendered_blank() -> None:
    overlay = public_overlay()
    broken = overlay.model_copy(
        update={"templates": {**overlay.templates, "friction-scan": "uses @@nope@@"}}
    )
    with pytest.raises(ValueError, match="unknown friction prompt field: nope"):
        asyncio.run(
            HandlerMorningFrictionSweep(None, FixtureGateway(), broken).handle(
                request({"force": True})
            )
        )

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The generator's reduction and gate, on synthetic walker output."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_event_chain_generator_compute.handlers.handler_event_chain_generator import (
    HandlerEventChainGenerator,
    UndrivablePathError,
)
from omnimarket.nodes.node_event_chain_generator_compute.models.model_chain_generation import (
    ChainKind,
    ModelChainExpectationSet,
    ModelChainGateRequest,
    ModelChainGateResult,
    ModelChainObligation,
    ModelDrivenChain,
    ModelGeneratedChain,
)
from tests.chains.delegation.obligations import load_obligations

pytestmark = pytest.mark.unit

OWNER = "node_x_orchestrator"
H = HandlerEventChainGenerator()


def _step(frm: str, trigger: str, to: str) -> dict[str, object]:
    return {"from_state": [frm, "r"], "trigger": trigger, "to_state": [to, "r"]}


REPORT = {
    "workflows": [
        {
            "workflow_owner": OWNER,
            "paths": [
                {
                    "kind": "golden",
                    "steps": [_step("A", "go", "B"), _step("B", "done", "C")],
                },
                # Same owner path, a reducer-only step: deduplicates onto the first.
                {
                    "kind": "golden",
                    "steps": [
                        _step("A", "go", "B"),
                        _step("B", "tick", "B"),
                        _step("B", "done", "C"),
                    ],
                },
                {"kind": "error", "steps": [_step("A", "boom", "F")]},
            ],
        }
    ]
}


def _chain(
    path_id: str, kind: ChainKind = "golden", events: tuple[str, ...] = ("E1",)
) -> ModelGeneratedChain:
    return ModelGeneratedChain(
        path_id=path_id,
        kind=kind,
        expected_event_types=events,
        expected_states=("B",),
        terminal_fields={"quality_passed": True},
    )


def test_obligations_project_onto_owner_and_deduplicate() -> None:
    obligations = H.obligations(REPORT, OWNER)
    assert [o.path_id for o in obligations] == ["error:boom", "golden:go>done"]
    assert obligations[1].steps == (("A", "go", "B"), ("B", "done", "C"))


def test_obligations_agree_with_the_pilot_reduction(tmp_path: Path) -> None:
    pilot = {o.path_id for o in load_obligations()}
    assert len(pilot) == 11
    # The pilot file is a reduction of the same walker; its ids are the
    # generator's ids by construction of the projection.
    report = json.loads(
        Path("tests/chains/delegation/walker_report.json").read_text(encoding="utf-8")
    )
    assert {p["path_id"] for p in report["paths"]} == pilot


class _Driver:
    workflow_owner = OWNER

    async def drive(self, obligation: ModelChainObligation) -> ModelDrivenChain:
        if "boom" in obligation.triggers:
            raise UndrivablePathError("no fixture for trigger 'boom'")
        return ModelDrivenChain(
            event_types=("E1", "E2"),
            states=("B", "C"),
            terminal_payload={"quality_passed": True, "prompt": "not a terminal fact"},
        )


async def test_generate_writes_one_chain_per_drivable_path_and_names_the_rest() -> None:
    generated = await H.generate(H.obligations(REPORT, OWNER), _Driver())
    assert [c.path_id for c in generated.chains] == ["golden:go>done"]
    assert generated.chains[0].expected_event_types == ("E1", "E2")
    assert generated.chains[0].terminal_fields == {"quality_passed": True}
    assert set(generated.undriven) == {"error:boom"}


def _gate(
    committed: tuple[ModelGeneratedChain, ...],
    generated: tuple[ModelGeneratedChain, ...],
    undriven: dict[str, str] | None = None,
) -> ModelChainGateResult:
    obligations = H.obligations(REPORT, OWNER)
    return H.gate(
        ModelChainGateRequest(
            workflow_owner=OWNER,
            obligations=obligations,
            committed=ModelChainExpectationSet(workflow_owner=OWNER, chains=committed),
            generated=ModelChainExpectationSet(
                workflow_owner=OWNER, chains=generated, undriven=undriven or {}
            ),
        )
    )


def test_gate_passes_when_every_path_has_its_matching_chain() -> None:
    chains = (_chain("error:boom", "error"), _chain("golden:go>done"))
    result = _gate(chains, chains)
    assert result.passed


def test_gate_fails_when_a_committed_path_has_no_fresh_drive() -> None:
    chains = (_chain("error:boom", "error"), _chain("golden:go>done"))
    result = _gate(chains, ())
    assert not result.passed
    assert result.undriven_path_ids == ("error:boom", "golden:go>done")


def test_gate_fails_on_a_new_path_with_no_chain() -> None:
    chains = (_chain("golden:go>done"),)
    result = _gate(chains, chains, {"error:boom": "no fixture for trigger 'boom'"})
    assert not result.passed
    assert result.missing_chain_path_ids == ("error:boom",)
    assert result.undriven_path_ids == ("error:boom",)


def test_gate_fails_on_a_chain_whose_path_is_gone() -> None:
    chains = (
        _chain("error:boom", "error"),
        _chain("golden:go>done"),
        _chain("golden:old"),
    )
    result = _gate(chains, chains)
    assert not result.passed
    assert result.stale_chain_path_ids == ("golden:old",)


def test_gate_fails_when_a_drive_no_longer_matches_its_chain() -> None:
    committed = (_chain("error:boom", "error"), _chain("golden:go>done"))
    generated = (
        _chain("error:boom", "error"),
        _chain("golden:go>done", events=("E1", "E9")),
    )
    result = _gate(committed, generated)
    assert not result.passed
    assert [(m.path_id, m.field) for m in result.mismatches] == [
        ("golden:go>done", "expected_event_types")
    ]

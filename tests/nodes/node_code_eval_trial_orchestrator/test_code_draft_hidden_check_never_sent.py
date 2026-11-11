# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23882 -- the draft request of every try carries no contract check, none of the check
files' paths and no held answer (plan rule 4; OMN-23627 AC16 "leak a contract check into the
draft"). The checks of a draft are the overlay's, never the ticket contract's."""

from __future__ import annotations

import pytest
from omnibase_core.enums.enum_delegation_traffic_class import EnumDelegationTrafficClass

from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration import leg
from tests.nodes.node_code_eval_trial_orchestrator.orchestrator_support import (
    Bus,
    make_bus,
    run_request,
)

pytestmark = pytest.mark.unit


def golden_bus() -> Bus:
    bus = make_bus()
    bus.send(run_request(2, 2))
    bus.drain()
    return bus


def assert_nothing_hidden_is_sent(bus: Bus) -> None:
    assert bus.units
    secrets = set()
    for material in bus.reader.read(bus.reader_candidates):  # type: ignore[attr-defined]
        secrets |= {
            material.held_diff,
            material.contract.path,
            *(f.path for f in material.check_files),
            *(f.sha256 for f in material.check_files),
        }
    assert secrets
    for unit in bus.units:
        wire = unit.model_dump_json()
        for secret in secrets:
            assert secret not in wire, f"{secret!r} reached the draft request"
        assert unit.code_draft is not None
        assert [c.argv for c in unit.code_draft.checks] == [
            ("uv", "run", "python", "-c", "pass")
        ]


def test_no_contract_check_check_path_or_held_answer_reaches_a_draft_request() -> None:
    bus = golden_bus()
    bus.reader_candidates = tuple(run_request(2, 2).candidates)  # type: ignore[attr-defined]
    assert_nothing_hidden_is_sent(bus)


def test_every_unit_carries_the_synthetic_traffic_class_and_the_orchestrators_surface() -> (
    None
):
    bus = golden_bus()
    for unit in bus.units:
        assert unit.code_draft is not None
        assert unit.code_draft.traffic_class is EnumDelegationTrafficClass.SYNTHETIC
        assert unit.code_draft.source_surface == "node_code_eval_trial_orchestrator"


def test_break_leak_a_contract_check_into_the_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = leg.build_unit_request

    def leaking(run_id, place, material, overlay):
        unit = real(run_id, place, material, overlay)
        draft = unit.code_draft.model_copy(
            update={"task": unit.code_draft.task + " " + material.contract.path}
        )
        return unit.model_copy(update={"code_draft": draft})

    monkeypatch.setattr(leg, "build_unit_request", leaking)
    bus = golden_bus()
    bus.reader_candidates = tuple(run_request(2, 2).candidates)  # type: ignore[attr-defined]
    with pytest.raises(AssertionError):
        assert_nothing_hidden_is_sent(bus)

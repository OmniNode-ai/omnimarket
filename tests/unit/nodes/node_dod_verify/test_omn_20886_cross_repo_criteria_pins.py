# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20886 -- a ticket whose criteria span repositories keeps all of its pins.

The DurableEvidenceGate unions bindings across a ticket's engaged repository
contracts; the criteria-drift check used to keep only the last repository's
pins, so every criterion another repository declared read as ADDED and a
verified verdict was demoted to skipped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_criteria_drift_kind import EnumCriteriaDriftKind
from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    ModelDodVerifyState,
)
from omnimarket.nodes.node_dod_verify.services.criteria_drift import (
    extract_criteria_pins,
    merge_criteria_pins,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)
from omnimarket.occ_contract_pin import criterion_hash
from tests.unit.nodes.node_dod_verify.omn_20858_ticket_reader import (
    RecordedTicketReader,
    criteria_body,
)
from tests.unit.nodes.node_dod_verify.omn_20886_repo_world import World

pytestmark = pytest.mark.unit

_AUDIENCE = EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE
_TEXT = {
    "AC1": "AC1: first -- falsifier: uv run pytest a.py",
    "AC2": "AC2: second -- falsifier: uv run pytest b.py",
    "AC3": "AC3: third -- falsifier: uv run pytest c.py",
}


def _contract(ticket: str, item_id: str, labels: list[str]) -> str:
    return yaml.safe_dump(
        {
            "schema_version": "1.0.0",
            "ticket_id": ticket,
            "dod_evidence": [
                {
                    "id": item_id,
                    "description": "the bound behaviour",
                    "source": "manual",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                    "binds_ac": labels,
                    "ac_bindings": [
                        {
                            "label": label,
                            "criterion_hash": criterion_hash(_TEXT[label]),
                            "proposed_by": "lane-author",
                            "accepted_by": "lane-reviewer",
                        }
                        for label in labels
                    ],
                }
            ],
        },
        sort_keys=False,
    )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _verify(
    world: World, monkeypatch: pytest.MonkeyPatch, ticket: str, labels: list[str]
) -> ModelDodVerifyState:
    world.install(monkeypatch)
    reader = RecordedTicketReader([criteria_body({k: _TEXT[k] for k in labels})])
    collector = EvidenceCollector(ticket_reader=reader)
    monkeypatch.setattr(
        HandlerDodVerify, "_make_collector", staticmethod(lambda: collector)
    )
    state = HandlerDodVerify().handle(
        ModelDodVerifyStartCommand(ticket_id=ticket, execution_audience=_AUDIENCE)
    )
    assert isinstance(state, ModelDodVerifyState)
    return state


def test_criteria_split_across_two_repos_stay_verified(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90301"
    world.merge(
        "omnidash",
        11,
        f"feat({ticket}): dash",
        {f"contracts/{ticket}.yaml": _contract(ticket, "dod-dash", ["AC1"])},
    )
    world.merge(
        "omnimarket",
        12,
        f"feat({ticket}): market",
        {f"contracts/{ticket}.yaml": _contract(ticket, "dod-market", ["AC2", "AC3"])},
    )

    state = _verify(world, monkeypatch, ticket, ["AC1", "AC2", "AC3"])

    assert state.criteria_drift == ()
    assert state.status is EnumDodVerifyStatus.VERIFIED


def test_a_criterion_no_repo_declares_is_still_added(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90302"
    world.merge(
        "omnidash",
        21,
        f"feat({ticket}): dash",
        {f"contracts/{ticket}.yaml": _contract(ticket, "dod-dash", ["AC1"])},
    )
    world.merge(
        "omnimarket",
        22,
        f"feat({ticket}): market",
        {f"contracts/{ticket}.yaml": _contract(ticket, "dod-market", ["AC2"])},
    )

    state = _verify(world, monkeypatch, ticket, ["AC1", "AC2", "AC3"])

    assert [(d.label, d.kind) for d in state.criteria_drift] == [
        ("AC3", EnumCriteriaDriftKind.ADDED)
    ]


def test_single_repo_pins_are_exactly_the_contracts_own(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket = "OMN-90303"
    text = _contract(ticket, "dod-market", ["AC1", "AC2"])
    world.merge(
        "omnimarket", 31, f"feat({ticket}): market", {f"contracts/{ticket}.yaml": text}
    )
    world.install(monkeypatch)
    reader = RecordedTicketReader(
        [criteria_body({k: _TEXT[k] for k in ("AC1", "AC2")})]
    )
    collector = EvidenceCollector(ticket_reader=reader)

    collector.collect(ticket, execution_audience=_AUDIENCE)

    raw: dict[str, Any] = yaml.safe_load(text)
    assert collector._criteria_pins == extract_criteria_pins(raw, raw["dod_evidence"])
    assert reader.reads == 1


def test_merge_of_one_part_is_the_part_unchanged() -> None:
    raw = yaml.safe_load(_contract("OMN-90304", "dod-x", ["AC1"]))
    pins = extract_criteria_pins(raw, raw["dod_evidence"])
    assert pins is not None
    assert merge_criteria_pins([("omnimarket", pins)]) == pins

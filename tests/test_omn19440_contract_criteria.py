# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19440 evidence declares and claims every acceptance criterion of the ticket."""

from pathlib import Path

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-19440.yaml"
CRITERIA = {"AC1", "AC2", "AC3", "AC4"}


def _load() -> ModelTicketContract:
    return ModelTicketContract.from_yaml(CONTRACT.read_text(encoding="utf-8"))


def test_contract_declares_every_ticket_criterion() -> None:
    contract = _load()
    declared = {
        criterion.id
        for requirement in contract.requirements
        for criterion in requirement.acceptance
    }
    assert declared == CRITERIA


def test_every_criterion_is_claimed_by_an_item_with_a_check() -> None:
    # An accepted binding record claims a criterion beside its original item,
    # so a criterion may be claimed more than once, never zero times.
    contract = _load()
    claimed: set[str] = set()
    for item in contract.dod_evidence:
        for label in item.binds_ac:
            claimed.add(label)
            assert item.checks, item.id
    assert claimed == CRITERIA


def test_each_criterion_names_its_own_test_class() -> None:
    contract = _load()
    classes = {
        "AC1": "TestAC1PlantedDropAndDoubleAreReported",
        "AC2": "TestAC2UnreadableIsIndeterminateNeverZero",
        "AC3": "TestAC3CountsPerCommandId",
        "AC4": "TestAC4PublishedSummaryHeadline",
    }
    for item in contract.dod_evidence:
        for label in item.binds_ac:
            assert any(classes[label] in check.check_value for check in item.checks), (
                item.id
            )

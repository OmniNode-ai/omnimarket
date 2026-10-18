# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19267 evidence claims only the ticket's current acceptance criteria."""

from pathlib import Path

from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-19267.yaml"


def test_contract_declares_and_binds_only_the_current_ticket_criteria() -> None:
    contract = ModelTicketContract.from_yaml(CONTRACT.read_text(encoding="utf-8"))
    declared = {
        criterion.id
        for requirement in contract.requirements
        for criterion in requirement.acceptance
    }
    claimed = {label for item in contract.dod_evidence for label in item.binds_ac}

    assert declared == {"AC1", "AC2"}
    assert claimed == declared


def test_historical_evidence_keeps_checks_without_criterion_claims() -> None:
    contract = ModelTicketContract.from_yaml(CONTRACT.read_text(encoding="utf-8"))
    items = {item.id: item for item in contract.dod_evidence}
    historical_checks = {
        "dod-derived-falsifier-id-collision": (
            "uv run pytest tests/test_dod_verify_acceptance_basis.py "
            "tests/test_occ_falsifier_checks.py -q -k id_collision"
        ),
        "dod-receipt-runner-one-supersession-per-check": (
            "uv run pytest "
            "tests/unit/scripts/test_occ_receipt_runner_shared_check_omn_19267.py -q"
        ),
        "dod-omn19267-ac6-reasoning-thinking-budget": (
            "uv run pytest "
            "tests/unit/delegation/test_reasoning_thinking_budget_omn19267.py"
        ),
    }

    for evidence_id, command in historical_checks.items():
        item = items[evidence_id]
        assert not item.binds_ac, evidence_id
        assert any(
            check.check_type == "test_passes" and check.check_value == command
            for check in item.checks
        ), evidence_id

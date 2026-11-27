# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-17461 contract carries the integration proof a runtime push owes.

The hook-push-rehearsal job pushes a sample change to a runtime path with this
branch's own ticket contract in the tree, and the ticket-contract prerequisite
hook refuses that push when the contract has no integration proof. Expect the
real contract to pass that check, and a copy without the proof item to fail it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.handler_integration_proof import (
    check_integration_proof,
)
from omnimarket.nodes.node_ticket_contract_prerequisite_effect.models.model_integration_proof_request import (
    ModelIntegrationProofRequest,
)

pytestmark = pytest.mark.unit

TICKET = "OMN-17461"
SAMPLE_RUNTIME_PATH = "src/omnimarket/adapters/adk/models.py"
CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / f"{TICKET}.yaml"


def _request(text: str) -> ModelIntegrationProofRequest:
    return ModelIntegrationProofRequest(
        ticket_id=TICKET,
        contract_text=text,
        changed_paths=(SAMPLE_RUNTIME_PATH,),
    )


def test_contract_carries_an_integration_proof_for_a_runtime_push() -> None:
    result = check_integration_proof(_request(CONTRACT.read_text(encoding="utf-8")))
    assert result.failure in (None, "")
    assert result.proof_item_ids


def test_contract_without_the_proof_item_is_refused() -> None:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    raw["dod_evidence"] = [
        item
        for item in raw["dod_evidence"]
        if "integration-proof" not in str(item.get("id", ""))
    ]
    result = check_integration_proof(_request(yaml.safe_dump(raw)))
    assert result.failure
    assert not result.proof_item_ids

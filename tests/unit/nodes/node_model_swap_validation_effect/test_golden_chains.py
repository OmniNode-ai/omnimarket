# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-level evidence cases exercise every shared terminal outcome."""

import json
from pathlib import Path

import pytest
from omnibase_core.enums.enum_model_swap_validation_failure import (
    EnumModelSwapValidationFailure,
)
from omnibase_core.models.model_swap_validation import ModelModelSwapValidationRequest

from omnimarket.nodes.node_model_swap_validation_effect.adapters.rightmodeler.validator import (
    ValidatorRightModeler,
)
from omnimarket.nodes.node_model_swap_validation_effect.handlers.handler_model_swap_validation import (
    HandlerModelSwapValidation,
    load_contract,
)
from omnimarket.nodes.node_model_swap_validation_effect.models.model_model_swap_service_config import (
    ModelModelSwapServiceConfig,
)

from .test_handler_model_swap_validation import contract_path
from .test_service_validator import Transport

_CHAINS = Path(__file__).resolve().parents[3] / "golden_chains/model_swap_validation"


@pytest.mark.parametrize(
    "path", sorted(_CHAINS.glob("chain_*.json")), ids=lambda p: p.stem
)
async def test_service_terminal_chain_is_durable(path: Path, tmp_path: Path) -> None:
    fixture = json.loads(path.read_text())
    request = ModelModelSwapValidationRequest.model_validate(fixture["request"])
    service = load_contract()["service"]
    service.update(
        endpoint_ref="https://service.example/validate",
        secret_ref="validation.service.key",
    )
    transport = Transport(json.dumps(fixture["service_terminal"]).encode())
    validator = ValidatorRightModeler(
        ModelModelSwapServiceConfig.model_validate(service), transport=transport
    )
    handler = HandlerModelSwapValidation(
        validator=validator, contract_path=contract_path(tmp_path)
    )
    terminal = await handler.handle(request)
    expected = fixture["expected"]
    actual = terminal.model_dump(mode="json")
    for name in ("status", "failure_reason", "decision", "evidence_only"):
        assert actual[name] == expected[name]
    assert actual["correlation_id"] == str(request.correlation_id)
    assert len(transport.requests) == expected["service_calls"]
    assert (
        await handler.handle(request)
    ).model_dump_json() == terminal.model_dump_json()
    assert len(transport.requests) == expected["service_calls"]


def test_chain_cases_cover_every_failure_and_both_decisions() -> None:
    fixtures = [json.loads(p.read_text()) for p in _CHAINS.glob("chain_*.json")]
    assert len(fixtures) == 12
    assert {
        f["expected"]["failure_reason"]
        for f in fixtures
        if f["expected"]["failure_reason"]
    } == {r.value for r in EnumModelSwapValidationFailure}
    assert {
        f["expected"]["decision"] for f in fixtures if f["expected"]["decision"]
    } == {"recommend", "reject"}

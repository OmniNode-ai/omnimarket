# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerTestGenerator (OMN-11677).

Verifies:
  - Same contract → same hash (determinism)
  - Output passes AST parse (syntax validity)
  - Generated test asserts declared model names from contract
  - Different contracts → different hashes
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime

import pytest
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

from omnimarket.nodes.node_test_generator.handlers.handler_test_generator import (
    HandlerTestGenerator,
)
from omnimarket.nodes.node_test_generator.models.model_test_generation_request import (
    ModelTestGenerationRequest,
)


def _make_contract(
    ticket_id: str = "OMN-99999",
    title: str = "Test ticket",
    node_type: str = "compute",
    input_model: str = "ModelFooRequest",
    output_model: str = "ModelFooResult",
) -> ModelTicketContract:
    return ModelTicketContract(
        ticket_id=ticket_id,
        title=title,
        context={
            "node_type": node_type,
            "input_model": input_model,
            "output_model": output_model,
        },
        created_at=datetime(2026, 5, 22, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 5, 22, 0, 0, 0, tzinfo=UTC),
    )


def _make_request(
    contract: ModelTicketContract | None = None,
    generator_version: str = "1.0.0",
    generation_profile_hash: str = "default",
) -> ModelTestGenerationRequest:
    return ModelTestGenerationRequest(
        contract=contract or _make_contract(),
        generator_version=generator_version,
        generation_profile_hash=generation_profile_hash,
    )


@pytest.mark.unit
def test_determinism_same_contract_same_hash() -> None:
    """Same contract + same generator_version → same test_hash on repeated invocation."""
    handler = HandlerTestGenerator()
    req = _make_request()
    result1 = handler.handle(req)
    result2 = handler.handle(req)
    assert result1.test_hash == result2.test_hash
    assert result1.test_source == result2.test_source
    assert result1.contract_hash == result2.contract_hash
    assert result1.template_hash == result2.template_hash


@pytest.mark.unit
def test_output_passes_ast_parse() -> None:
    """Generated test source must be valid Python (AST parseable)."""
    handler = HandlerTestGenerator()
    result = handler.handle(_make_request())
    # If this raises SyntaxError, ast.parse will propagate it.
    tree = ast.parse(result.test_source)
    assert tree is not None


@pytest.mark.unit
def test_generated_test_asserts_declared_model_names() -> None:
    """Generated test source must reference the contract's declared model names."""
    contract = _make_contract(
        input_model="ModelSpecificRequest",
        output_model="ModelSpecificResult",
    )
    handler = HandlerTestGenerator()
    result = handler.handle(_make_request(contract=contract))
    assert "ModelSpecificRequest" in result.test_source
    assert "ModelSpecificResult" in result.test_source


@pytest.mark.unit
def test_different_contracts_different_hashes() -> None:
    """Different contracts must produce different test_hash values."""
    contract_a = _make_contract(ticket_id="OMN-00001", title="First ticket")
    contract_b = _make_contract(ticket_id="OMN-00002", title="Second ticket")
    handler = HandlerTestGenerator()
    result_a = handler.handle(_make_request(contract=contract_a))
    result_b = handler.handle(_make_request(contract=contract_b))
    assert result_a.test_hash != result_b.test_hash
    assert result_a.contract_hash != result_b.contract_hash


@pytest.mark.unit
def test_template_hash_stable() -> None:
    """Template hash must be stable (same template → same hash) across calls."""
    handler = HandlerTestGenerator()
    result1 = handler.handle(_make_request())
    result2 = handler.handle(_make_request())
    assert result1.template_hash == result2.template_hash


@pytest.mark.unit
def test_result_fields_populated() -> None:
    """All result fields must be non-empty strings."""
    handler = HandlerTestGenerator()
    result = handler.handle(_make_request())
    assert result.test_source
    assert result.test_hash
    assert result.contract_hash
    assert result.generator_version == "1.0.0"
    assert result.template_hash
    assert result.generation_profile_hash == "default"
    assert result.generated_at


@pytest.mark.unit
def test_different_generator_versions_different_results() -> None:
    """Different generator_version values may differ in metadata even with same contract."""
    handler = HandlerTestGenerator()
    result_v1 = handler.handle(_make_request(generator_version="1.0.0"))
    result_v2 = handler.handle(_make_request(generator_version="2.0.0"))
    # generator_version appears in the template output, so test_hash must differ
    assert result_v1.test_hash != result_v2.test_hash
    assert result_v1.generator_version == "1.0.0"
    assert result_v2.generator_version == "2.0.0"


@pytest.mark.unit
def test_subscribe_topics_in_generated_test() -> None:
    """Generated test must reference the contract subscribe topics."""
    handler = HandlerTestGenerator()
    result = handler.handle(_make_request())
    # The template includes a test for subscribe_topics
    assert "subscribe_topics" in result.test_source
    assert "onex.cmd.omnimarket.test-generation-requested.v1" in result.test_source


@pytest.mark.unit
def test_publish_topics_in_generated_test() -> None:
    """Generated test must reference the contract publish topics."""
    handler = HandlerTestGenerator()
    result = handler.handle(_make_request())
    assert "publish_topics" in result.test_source
    assert "onex.evt.omnimarket.test-generation-completed.v1" in result.test_source

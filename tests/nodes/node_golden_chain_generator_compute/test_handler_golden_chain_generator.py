"""Tests for deterministic golden-chain generation."""

from __future__ import annotations

import pytest
from omnibase_core.models.ticket.model_golden_path import ModelGoldenPath
from omnibase_core.models.ticket.model_golden_path_assertion import (
    ModelGoldenPathAssertion,
)
from omnibase_core.models.ticket.model_golden_path_input import ModelGoldenPathInput
from omnibase_core.models.ticket.model_golden_path_output import ModelGoldenPathOutput
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

from omnimarket.nodes.node_golden_chain_generator_compute.handlers.handler_golden_chain_generator import (
    HandlerGoldenChainGenerator,
)
from omnimarket.nodes.node_golden_chain_generator_compute.models.model_golden_chain_generation_request import (
    ModelGoldenChainGenerationRequest,
)
from omnimarket.nodes.node_golden_chain_generator_compute.models.model_golden_chain_generation_result import (
    EnumGoldenChainGenerationStatus,
)


def _contract_with_golden_path() -> ModelTicketContract:
    contract = ModelTicketContract(
        ticket_id="OMN-11697",
        title="Context Pack Pipeline",
        golden_path=ModelGoldenPath(
            input=ModelGoldenPathInput(
                topic="onex.cmd.omnimarket.context-pack-requested.v1",
                fixture="fixtures/context-pack-request.json",
            ),
            output=ModelGoldenPathOutput(
                topic="onex.evt.omnimarket.context-pack-completed.v1",
                assertions=[
                    ModelGoldenPathAssertion(
                        field="status",
                        op="eq",
                        value="ok",
                    )
                ],
            ),
        ),
    )
    contract.update_fingerprint()
    return contract


@pytest.mark.unit
class TestHandlerGoldenChainGenerator:
    def test_derives_chain_entries_from_explicit_golden_path_topics(self) -> None:
        result = HandlerGoldenChainGenerator().handle(
            ModelGoldenChainGenerationRequest(contract=_contract_with_golden_path())
        )

        assert result.status == EnumGoldenChainGenerationStatus.DEFERRED
        assert len(result.expected_chain) == 2
        assert result.expected_chain[0].sequence == 1
        assert result.expected_chain[0].event_type == "golden_path_input"
        assert (
            result.expected_chain[0].topic
            == "onex.cmd.omnimarket.context-pack-requested.v1"
        )
        assert result.expected_chain[0].source_node == "UNKNOWN"
        assert result.expected_chain[1].event_type == "golden_path_output"
        assert result.chain_hash

    def test_unknown_topology_is_warning_not_inferred_node(self) -> None:
        result = HandlerGoldenChainGenerator().handle(
            ModelGoldenChainGenerationRequest(contract=_contract_with_golden_path())
        )

        warning_codes = {warning.code for warning in result.deferred_warnings}
        assert "SOURCE_NODE_UNKNOWN" in warning_codes
        assert "ASSERTIONS_NOT_TOPOLOGY" in warning_codes

    def test_same_input_produces_same_chain_hash(self) -> None:
        handler = HandlerGoldenChainGenerator()
        request = ModelGoldenChainGenerationRequest(
            contract=_contract_with_golden_path(),
            generated_test_hash="abc123",
            template_hash="template123",
        )

        first = handler.handle(request)
        second = handler.handle(request)

        assert first.chain_hash == second.chain_hash
        assert first.contract_hash == second.contract_hash

    def test_missing_golden_path_is_insufficient_contract(self) -> None:
        contract = ModelTicketContract(
            ticket_id="OMN-EMPTY",
            title="No golden path",
        )

        result = HandlerGoldenChainGenerator().handle(
            ModelGoldenChainGenerationRequest(contract=contract)
        )

        assert result.status == EnumGoldenChainGenerationStatus.INSUFFICIENT_CONTRACT
        assert result.expected_chain == ()
        assert result.deferred_warnings[0].code == "GOLDEN_PATH_MISSING"

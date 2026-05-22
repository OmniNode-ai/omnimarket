"""Tests for deterministic ticket-contract test generation."""

from __future__ import annotations

import ast

import pytest
from omnibase_core.enums.ticket.enum_dod_check_type import EnumDodCheckType
from omnibase_core.enums.ticket.enum_evidence_kind import EnumEvidenceKind
from omnibase_core.models.contracts.ticket.model_dod_evidence_check import (
    ModelDodEvidenceCheck,
)
from omnibase_core.models.ticket.model_contract_dod_item import ModelContractDodItem
from omnibase_core.models.ticket.model_evidence_requirement import (
    ModelEvidenceRequirement,
)
from omnibase_core.models.ticket.model_golden_path import ModelGoldenPath
from omnibase_core.models.ticket.model_golden_path_input import ModelGoldenPathInput
from omnibase_core.models.ticket.model_golden_path_output import ModelGoldenPathOutput
from omnibase_core.models.ticket.model_requirement import ModelRequirement
from omnibase_core.models.ticket.model_ticket_contract import ModelTicketContract

from omnimarket.nodes.node_test_generator_compute.handlers.handler_test_generator import (
    HandlerTestGenerator,
)
from omnimarket.nodes.node_test_generator_compute.models.model_test_generation_request import (
    ModelTestGenerationRequest,
)
from omnimarket.nodes.node_test_generator_compute.models.model_test_generation_result import (
    EnumTestGenerationStatus,
)


def _contract() -> ModelTicketContract:
    contract = ModelTicketContract(
        ticket_id="OMN-11697",
        title="Context Pack Pipeline",
        requirements=[
            ModelRequirement(
                id="req-1",
                statement="Generate contract-driven acceptance artifacts.",
                acceptance=[
                    {
                        "id": "ac-1",
                        "statement": "Generated tests preserve contract refs.",
                    }
                ],
            )
        ],
        golden_path=ModelGoldenPath(
            input=ModelGoldenPathInput(
                topic="onex.cmd.omnimarket.example-requested.v1",
                fixture="fixtures/example.json",
            ),
            output=ModelGoldenPathOutput(
                topic="onex.evt.omnimarket.example-completed.v1",
            ),
        ),
        dod_evidence=[
            ModelContractDodItem(
                id="dod-1",
                description="Focused tests pass.",
                checks=[
                    ModelDodEvidenceCheck(
                        check_type=EnumDodCheckType.TEST_PASSES,
                        check_value="uv run pytest tests/nodes",
                    )
                ],
            )
        ],
        evidence_requirements=[
            ModelEvidenceRequirement(
                kind=EnumEvidenceKind.TESTS,
                description="Focused pytest output is captured.",
            )
        ],
    )
    contract.update_fingerprint()
    return contract


@pytest.mark.unit
class TestHandlerTestGenerator:
    def test_generates_parser_valid_test_artifact(self) -> None:
        result = HandlerTestGenerator().handle(
            ModelTestGenerationRequest(contract=_contract())
        )

        assert result.status == EnumTestGenerationStatus.OK
        assert len(result.generated_files) == 1
        generated_file = result.generated_files[0]
        ast.parse(generated_file.content)
        assert generated_file.path == "generated_tests/test_omn_11697_contract.py"
        assert generated_file.content_sha256
        assert generated_file.pytest_node_ids

    def test_preserves_requirement_and_acceptance_source_refs(self) -> None:
        result = HandlerTestGenerator().handle(
            ModelTestGenerationRequest(contract=_contract())
        )

        generated_file = result.generated_files[0]
        assert "requirement:req-1" in generated_file.source_refs
        assert "acceptance:req-1/ac-1" in generated_file.source_refs
        assert "dod:dod-1" in generated_file.source_refs
        assert "golden_path:input" in generated_file.source_refs

    def test_same_input_produces_same_hashes(self) -> None:
        handler = HandlerTestGenerator()
        request = ModelTestGenerationRequest(contract=_contract())

        first = handler.handle(request)
        second = handler.handle(request)

        assert first.contract_hash == second.contract_hash
        assert first.template_hash == second.template_hash
        assert first.generated_files[0].content_sha256 == (
            second.generated_files[0].content_sha256
        )

    def test_empty_contract_returns_insufficient_contract(self) -> None:
        contract = ModelTicketContract(
            ticket_id="OMN-EMPTY",
            title="No machine material",
        )

        result = HandlerTestGenerator().handle(
            ModelTestGenerationRequest(contract=contract)
        )

        assert result.status == EnumTestGenerationStatus.INSUFFICIENT_CONTRACT
        assert result.generated_files == ()
        assert result.warnings

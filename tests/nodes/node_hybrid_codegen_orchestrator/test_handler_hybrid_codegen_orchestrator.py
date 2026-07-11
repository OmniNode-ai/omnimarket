# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Phase-routing unit tests for the hybrid codegen ORCHESTRATOR (decoupled).

Each phase is driven in isolation with a synthetic stage outcome; the test
asserts the orchestrator emits the right next command on the right topic with the
right payload type, and that the rejection branches emit a terminal completed.
The bus-driven end-to-end wiring is covered by ``test_golden_chain_composition``;
the payload/topic seam is covered by ``test_seam_match``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.codegen.models import (
    EnumCodegenStatus,
    ModelCodegenCompleted,
    ModelCodegenPipelineState,
    ModelCodegenSerializeOutcome,
    ModelCodegenSpec,
    ModelCodegenTypecheckOutcome,
    ModelCodegenValidationOutcome,
    ModelContractAssemblyRequestSeam,
    ModelFileWriteCommand,
    ModelFileWriteResult,
    ModelLlmGenerateCommand,
    ModelLlmGenerateResult,
    ModelMypyRequestSeam,
    ModelValidatorRequestSeam,
)
from omnimarket.nodes.node_hybrid_codegen_orchestrator.handlers.handler_hybrid_codegen_orchestrator import (
    HandlerHybridCodegenOrchestrator,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hybrid_codegen_orchestrator"
    / "contract.yaml"
)


def _spec() -> ModelCodegenSpec:
    return ModelCodegenSpec(
        node_name="NodeGreeterCompute",
        namespace="omninode.services.greeter.compute",
        archetype="compute",
        base_class="NodeCompute",
        target_root="build/node_greeter_compute",
    )


def _state(source: str = "", contract_yaml: str = "") -> ModelCodegenPipelineState:
    return ModelCodegenPipelineState(
        spec=_spec(), source_text=source, contract_yaml=contract_yaml
    )


def _env(event_type: str, payload: object) -> ModelEventEnvelope[object]:
    return ModelEventEnvelope(
        payload=payload, correlation_id=uuid4(), event_type=event_type
    )


async def _handle(event_type: str, payload: object) -> ModelEventEnvelope[object]:
    output = await HandlerHybridCodegenOrchestrator().handle(_env(event_type, payload))
    assert len(output.events) == 1
    return output.events[0]


@pytest.mark.asyncio
class TestPhaseRouting:
    async def test_start_emits_llm_generate(self) -> None:
        event = await _handle("onex.cmd.omnimarket.hybrid-codegen-start.v1", _spec())
        assert event.event_type.endswith("codegen-llm-generate.v1")
        assert isinstance(event.payload, ModelLlmGenerateCommand)
        assert event.payload.state.spec.node_name == "NodeGreeterCompute"

    async def test_llm_generated_emits_validator_seam(self) -> None:
        payload = ModelLlmGenerateResult(state=_state(source="class X: ..."))
        event = await _handle("onex.evt.omnimarket.codegen-llm-generated.v1", payload)
        assert event.event_type.endswith("generated-code-validation-requested.v1")
        assert isinstance(event.payload, ModelValidatorRequestSeam)
        assert event.payload.source_text == "class X: ..."
        assert event.payload.expected is not None
        assert event.payload.expected.class_name == "NodeGreeterCompute"
        assert event.payload.expected.base_class == "NodeCompute"
        assert event.payload.expected.required_methods == ("handle",)

    async def test_valid_outcome_emits_mypy_seam(self) -> None:
        payload = ModelCodegenValidationOutcome(
            state=_state(source="code"), is_valid=True
        )
        event = await _handle(
            "onex.evt.omnimarket.codegen-validation-outcome.v1", payload
        )
        assert event.event_type.endswith("mypy-check-requested.v1")
        assert isinstance(event.payload, ModelMypyRequestSeam)
        assert event.payload.source_text == "code"

    async def test_invalid_outcome_emits_rejected_terminal(self) -> None:
        payload = ModelCodegenValidationOutcome(
            state=_state(), is_valid=False, issues=("bad",)
        )
        event = await _handle(
            "onex.evt.omnimarket.codegen-validation-outcome.v1", payload
        )
        assert event.event_type.endswith("hybrid-codegen-completed.v1")
        assert isinstance(event.payload, ModelCodegenCompleted)
        assert event.payload.status is EnumCodegenStatus.REJECTED_VALIDATION
        assert event.payload.issues == ("bad",)

    async def test_typecheck_success_emits_contract_serialize_seam(self) -> None:
        payload = ModelCodegenTypecheckOutcome(
            state=_state(source="code"), success=True
        )
        event = await _handle(
            "onex.evt.omnimarket.codegen-typecheck-outcome.v1", payload
        )
        assert event.event_type.endswith("contract-serialize-requested.v1")
        assert isinstance(event.payload, ModelContractAssemblyRequestSeam)
        assert event.payload.node_name == "NodeGreeterCompute"
        assert event.payload.archetype == "compute"

    async def test_typecheck_failure_emits_rejected_terminal(self) -> None:
        payload = ModelCodegenTypecheckOutcome(
            state=_state(), success=False, error_count=2
        )
        event = await _handle(
            "onex.evt.omnimarket.codegen-typecheck-outcome.v1", payload
        )
        assert isinstance(event.payload, ModelCodegenCompleted)
        assert event.payload.status is EnumCodegenStatus.REJECTED_TYPECHECK

    async def test_serialize_outcome_emits_file_write_with_files(self) -> None:
        payload = ModelCodegenSerializeOutcome(
            state=_state(source="handler-src", contract_yaml="name: x\n")
        )
        event = await _handle(
            "onex.evt.omnimarket.codegen-serialize-outcome.v1", payload
        )
        assert event.event_type.endswith("codegen-file-write.v1")
        assert isinstance(event.payload, ModelFileWriteCommand)
        by_path = {f.relative_path: f.content for f in event.payload.files}
        assert by_path["handler.py"] == "handler-src"
        assert by_path["contract.yaml"] == "name: x\n"
        assert "metadata.yaml" in by_path

    async def test_files_written_emits_completed_terminal(self) -> None:
        payload = ModelFileWriteResult(
            state=_state(), written_paths=("build/node/handler.py",)
        )
        event = await _handle("onex.evt.omnimarket.codegen-files-written.v1", payload)
        assert isinstance(event.payload, ModelCodegenCompleted)
        assert event.payload.status is EnumCodegenStatus.COMPLETED
        assert event.payload.written_paths == ("build/node/handler.py",)


@pytest.mark.unit
class TestContractTopicCoverage:
    """Cover the orchestrator's declared bus topology."""

    def test_contract_declares_pipeline_topics(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        publish = set(contract["event_bus"]["publish_topics"])
        subscribe = set(contract["event_bus"]["subscribe_topics"])
        assert {
            "onex.cmd.omnimarket.codegen-llm-generate.v1",
            "onex.cmd.omnimarket.generated-code-validation-requested.v1",
            "onex.cmd.omnimarket.mypy-check-requested.v1",
            "onex.cmd.omnimarket.contract-serialize-requested.v1",
            "onex.cmd.omnimarket.codegen-file-write.v1",
        } <= publish
        assert "onex.cmd.omnimarket.hybrid-codegen-start.v1" in subscribe
        assert contract["terminal_event"] == (
            "onex.evt.omnimarket.hybrid-codegen-completed.v1"
        )
        assert contract["descriptor"]["node_archetype"] == "orchestrator"

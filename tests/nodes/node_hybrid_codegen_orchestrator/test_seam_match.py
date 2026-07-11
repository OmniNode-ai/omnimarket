# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-level seam-match assertion (OMN-14208 guard, decoupled).

tier-4 depends on tier-1/2/3 ONLY through the topic + payload contract, never by
importing their handlers or co-locating their source. This test proves, at the
contract level, that each command the orchestrator publishes on a downstream
node's subscribe topic is a valid input for that node — field-by-field — without
any tier-1/2/3 code present.

The downstream expectations below are the SEAM CONTRACT tier-4 commits to. Each
mirrors the real consumer's request model (cited); the assertion fails if tier-4's
emitted payload drifts (adds a field the consumer forbids, or drops a required
one). When tier-1/2/3 co-deploy, the live topic wiring is the other half of the
seam; this contract-level half is what lets tier-4 land independently.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.codegen.models import (
    ModelCodegenPipelineState,
    ModelCodegenSerializeOutcome,
    ModelCodegenSpec,
    ModelCodegenTypecheckOutcome,
    ModelCodegenValidationOutcome,
    ModelLlmGenerateResult,
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

# The downstream SEAM CONTRACT: topic -> the consumer's accepted request fields.
# Sourced from the real consumer request models (which tier-4 must NOT import):
#   generated-code-validation-requested.v1 -> node_generated_code_validator
#       ModelGeneratedCodeValidatorRequest{source_text (required), expected?}
#   mypy-check-requested.v1 -> node_mypy_check_effect
#       ModelMypyCheckRequest{source_text? , path?, ignore_missing_imports?}
#       (exactly-one-of source_text/path; tier-4 always supplies source_text)
#   contract-serialize-requested.v1 -> node_contract_serialize_compute
#       ModelContractAssemblyRequest{node_name, namespace, archetype (required),
#                                    analysis?, subcontract_selections?, overrides?}
_SEAM_CONTRACT: dict[str, dict[str, set[str]]] = {
    "generated-code-validation-requested.v1": {
        "required": {"source_text"},
        "allowed": {"source_text", "expected"},
    },
    "mypy-check-requested.v1": {
        "required": {"source_text"},
        "allowed": {"source_text", "path", "ignore_missing_imports"},
    },
    "contract-serialize-requested.v1": {
        "required": {"node_name", "namespace", "archetype"},
        "allowed": {
            "node_name",
            "namespace",
            "archetype",
            "analysis",
            "subcontract_selections",
            "overrides",
        },
    },
}

# The nested `expected` object on the validator seam must match the consumer's
# ModelExpectedStructure{class_name?, base_class?, required_methods?}.
_VALIDATOR_EXPECTED_ALLOWED = {"class_name", "base_class", "required_methods"}


def _spec() -> ModelCodegenSpec:
    return ModelCodegenSpec(
        node_name="NodeGreeterCompute",
        namespace="omninode.services.greeter.compute",
        archetype="compute",
        base_class="NodeCompute",
        handler_method="handle",
        description="Greets a subject",
        target_root="build/node_greeter_compute",
    )


def _state(
    source: str = "code", contract_yaml: str = "name: x\n"
) -> ModelCodegenPipelineState:
    return ModelCodegenPipelineState(
        spec=_spec(), source_text=source, contract_yaml=contract_yaml
    )


def _env(event_type: str, payload: object) -> ModelEventEnvelope[object]:
    return ModelEventEnvelope(
        payload=payload, correlation_id=uuid4(), event_type=event_type
    )


async def _emit_for(event_type: str, payload: object) -> ModelEventEnvelope[object]:
    output = await HandlerHybridCodegenOrchestrator().handle(_env(event_type, payload))
    assert len(output.events) == 1
    return output.events[0]


def _assert_seam(topic_suffix: str, payload_dump: dict[str, object]) -> None:
    contract = next(
        spec for topic, spec in _SEAM_CONTRACT.items() if topic.endswith(topic_suffix)
    )
    keys = set(payload_dump)
    extra = keys - contract["allowed"]
    missing = contract["required"] - keys
    assert not extra, (
        f"{topic_suffix}: payload has fields the consumer forbids: {extra}"
    )
    assert not missing, (
        f"{topic_suffix}: payload missing required consumer fields: {missing}"
    )


@pytest.mark.asyncio
class TestSeamMatch:
    """tier-4's emitted payloads are valid inputs for each downstream node."""

    async def test_validator_seam_matches(self) -> None:
        emitted = await _emit_for(
            "onex.evt.omnimarket.codegen-llm-generated.v1",
            ModelLlmGenerateResult(state=_state()),
        )
        assert emitted.event_type.endswith("generated-code-validation-requested.v1")
        dump = emitted.payload.model_dump()
        _assert_seam("generated-code-validation-requested.v1", dump)
        # nested `expected` must match the consumer's ModelExpectedStructure fields.
        assert set(dump["expected"]) <= _VALIDATOR_EXPECTED_ALLOWED
        # and it must be populated from the spec (the real seam content).
        assert dump["expected"]["class_name"] == "NodeGreeterCompute"
        assert dump["expected"]["base_class"] == "NodeCompute"
        assert dump["expected"]["required_methods"] == ("handle",)

    async def test_mypy_seam_matches(self) -> None:
        emitted = await _emit_for(
            "onex.evt.omnimarket.codegen-validation-outcome.v1",
            ModelCodegenValidationOutcome(state=_state(), is_valid=True),
        )
        assert emitted.event_type.endswith("mypy-check-requested.v1")
        _assert_seam("mypy-check-requested.v1", emitted.payload.model_dump())

    async def test_contract_serialize_seam_matches(self) -> None:
        emitted = await _emit_for(
            "onex.evt.omnimarket.codegen-typecheck-outcome.v1",
            ModelCodegenTypecheckOutcome(state=_state(), success=True),
        )
        assert emitted.event_type.endswith("contract-serialize-requested.v1")
        dump = emitted.payload.model_dump()
        _assert_seam("contract-serialize-requested.v1", dump)
        # nested analysis must match the consumer's ModelNodeAnalysis shape.
        assert set(dump["analysis"]) <= {"description", "tags", "version"}

    async def test_serialize_outcome_drives_file_write_not_a_downstream_seam(
        self,
    ) -> None:
        # The file-write command targets tier-4's OWN effect, so it legitimately
        # carries pipeline state — assert the file set is assembled, not a seam.
        emitted = await _emit_for(
            "onex.evt.omnimarket.codegen-serialize-outcome.v1",
            ModelCodegenSerializeOutcome(state=_state(source="SRC", contract_yaml="Y")),
        )
        assert emitted.event_type.endswith("codegen-file-write.v1")
        by_path = {f.relative_path: f.content for f in emitted.payload.files}
        assert by_path["handler.py"] == "SRC"
        assert by_path["contract.yaml"] == "Y"


@pytest.mark.unit
class TestContractDeclaresSeamTopics:
    """Every seam topic tier-4 publishes to is declared in its contract."""

    def test_publish_topics_cover_every_downstream_seam(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        publish = set(contract["event_bus"]["publish_topics"])
        for topic in _SEAM_CONTRACT:
            assert f"onex.cmd.omnimarket.{topic}" in publish, topic

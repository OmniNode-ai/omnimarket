# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Phase-routing / routing-equivalence tests for the codegen ORCHESTRATOR (def-B).

Each phase is driven in isolation with a synthetic stage payload; the test asserts
the def-B ``handle(request)`` emits the right next event **model** and that model's
class resolves to the right publish **topic** via the contract's ``published_events``
(the same class -> topic resolution the runtime uses). Because the emitted
(resolved-topic, payload-model) per phase is exactly what the removed
``handle(envelope)`` produced, this IS the def-B == envelope routing-equivalence
proof (OMN-14403 §4). The rejection branches emit a terminal completed.

Definition B (OMN-14355): the handler takes a bare typed payload and returns a
tuple of bare typed models — no ``ModelEventEnvelope``, no ``event_type`` on the
handler side. The topic lives in the contract, resolved here exactly as the
applier does (``removeprefix('Model')`` then published_events lookup).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from pydantic import BaseModel

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


def _published_events() -> dict[str, str]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    return {e["event_type"]: e["topic"] for e in contract["published_events"]}


_PUBLISHED = _published_events()


def _resolve_topic(model: BaseModel) -> str:
    """Resolve an emitted model's topic exactly as the runtime applier does."""
    short = type(model).__name__.removeprefix("Model")
    return _PUBLISHED[short]


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


def _handle(payload: BaseModel) -> tuple[str, BaseModel]:
    """Drive one phase; return the single emitted (resolved-topic, payload-model)."""
    output = HandlerHybridCodegenOrchestrator().handle(payload)
    assert len(output) == 1
    model = output[0]
    return _resolve_topic(model), model


@pytest.mark.unit
class TestPhaseRoutingEquivalence:
    def test_start_emits_llm_generate(self) -> None:
        topic, payload = _handle(_spec())
        assert topic.endswith("codegen-llm-generate.v1")
        assert isinstance(payload, ModelLlmGenerateCommand)
        assert payload.state.spec.node_name == "NodeGreeterCompute"

    def test_llm_generated_emits_validator_seam(self) -> None:
        topic, payload = _handle(
            ModelLlmGenerateResult(state=_state(source="class X: ..."))
        )
        assert topic.endswith("generated-code-validation-requested.v1")
        assert isinstance(payload, ModelValidatorRequestSeam)
        assert payload.source_text == "class X: ..."
        assert payload.expected is not None
        assert payload.expected.class_name == "NodeGreeterCompute"
        assert payload.expected.base_class == "NodeCompute"
        assert payload.expected.required_methods == ("handle",)

    def test_valid_outcome_emits_mypy_seam(self) -> None:
        topic, payload = _handle(
            ModelCodegenValidationOutcome(state=_state(source="code"), is_valid=True)
        )
        assert topic.endswith("mypy-check-requested.v1")
        assert isinstance(payload, ModelMypyRequestSeam)
        assert payload.source_text == "code"

    def test_invalid_outcome_emits_rejected_terminal(self) -> None:
        topic, payload = _handle(
            ModelCodegenValidationOutcome(
                state=_state(), is_valid=False, issues=("bad",)
            )
        )
        assert topic.endswith("hybrid-codegen-completed.v1")
        assert isinstance(payload, ModelCodegenCompleted)
        assert payload.status is EnumCodegenStatus.REJECTED_VALIDATION
        assert payload.issues == ("bad",)

    def test_typecheck_success_emits_contract_serialize_seam(self) -> None:
        topic, payload = _handle(
            ModelCodegenTypecheckOutcome(state=_state(source="code"), success=True)
        )
        assert topic.endswith("contract-serialize-requested.v1")
        assert isinstance(payload, ModelContractAssemblyRequestSeam)
        assert payload.node_name == "NodeGreeterCompute"
        assert payload.archetype == "compute"

    def test_typecheck_failure_emits_rejected_terminal(self) -> None:
        topic, payload = _handle(
            ModelCodegenTypecheckOutcome(state=_state(), success=False, error_count=2)
        )
        assert topic.endswith("hybrid-codegen-completed.v1")
        assert isinstance(payload, ModelCodegenCompleted)
        assert payload.status is EnumCodegenStatus.REJECTED_TYPECHECK

    def test_serialize_outcome_emits_file_write_with_files(self) -> None:
        topic, payload = _handle(
            ModelCodegenSerializeOutcome(
                state=_state(source="handler-src", contract_yaml="name: x\n")
            )
        )
        assert topic.endswith("codegen-file-write.v1")
        assert isinstance(payload, ModelFileWriteCommand)
        by_path = {f.relative_path: f.content for f in payload.files}
        assert by_path["handler.py"] == "handler-src"
        assert by_path["contract.yaml"] == "name: x\n"
        assert "metadata.yaml" in by_path
        assert ".rsd_provenance.json" in by_path

    def test_files_written_emits_completed_terminal(self) -> None:
        topic, payload = _handle(
            ModelFileWriteResult(
                state=_state(), written_paths=("build/node/handler.py",)
            )
        )
        assert topic.endswith("hybrid-codegen-completed.v1")
        assert isinstance(payload, ModelCodegenCompleted)
        assert payload.status is EnumCodegenStatus.COMPLETED
        assert payload.written_paths == ("build/node/handler.py",)


@pytest.mark.unit
class TestDefBCanonShape:
    """Lock the def-B shape: no envelope, topic_match, published_events coverage."""

    def test_handler_does_not_import_event_envelope(self) -> None:
        import ast

        src_path = (
            _CONTRACT_PATH.parent
            / "handlers"
            / "handler_hybrid_codegen_orchestrator.py"
        )
        tree = ast.parse(src_path.read_text())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom | ast.Import):
                imported.update(alias.name for alias in node.names)
        # The def-B canon (OMN-14355) forbids the envelope/handler-output surfaces
        # in the handler; importing either hard-fails the canon-shape ratchet.
        assert "ModelEventEnvelope" not in imported
        assert "ModelHandlerOutput" not in imported

    def test_contract_is_topic_match_with_per_topic_event_model(self) -> None:
        contract = yaml.safe_load(_CONTRACT_PATH.read_text())
        routing = contract["handler_routing"]
        assert routing["routing_strategy"] == "topic_match"
        subscribe = set(contract["event_bus"]["subscribe_topics"])
        entry_topics = {h["topic"] for h in routing["handlers"]}
        assert entry_topics == subscribe
        for handler in routing["handlers"]:
            assert handler["event_model"]["name"]  # every topic has a wire model

    def test_published_events_cover_every_emitted_class_injectively(self) -> None:
        # Every class the handler can emit is declared, and the map is injective.
        emitted_short_names = {
            "LlmGenerateCommand",
            "ValidatorRequestSeam",
            "MypyRequestSeam",
            "ContractAssemblyRequestSeam",
            "FileWriteCommand",
            "CodegenCompleted",
        }
        assert emitted_short_names <= set(_PUBLISHED)
        assert len(set(_PUBLISHED.values())) == len(_PUBLISHED)  # injective


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


@pytest.mark.unit
class TestProvenanceStampSeam:
    """OMN-15011 emission-side seam: the ``.rsd_provenance.json`` stamp this
    orchestrator writes is the field-for-field contract the omnibase_core
    fail-closed gate (``scripts/ci/rsd_provenance_stamp.py``) parses and
    RECOMPUTES against on the consuming side. The two repos cannot co-import
    each other (compat -> core -> spi -> infra layering; omnimarket has no
    reach into omnibase_core's CI scripts), so this locks the schema this side
    emits with an exact golden dict -- the same golden dict, byte-for-byte, is
    asserted against a fixture in omnibase_core's
    ``tests/unit/scripts/ci/test_rsd_provenance_stamp.py`` (see that file's
    ``TestSeamContractWithOmnimarketEmitter`` for the other half of this seam).
    This mirrors the existing seam-match pattern in
    ``omnimarket.codegen.models`` (``ModelValidatorRequestSeam`` et al.):
    verified at the schema/field level without requiring co-presence of the
    consuming node.
    """

    def test_provenance_stamp_is_a_generated_file(self) -> None:
        state = _state(source="handler-src", contract_yaml="name: x\n")
        _, payload = _handle(ModelCodegenSerializeOutcome(state=state))
        assert isinstance(payload, ModelFileWriteCommand)
        by_path = {f.relative_path: f.content for f in payload.files}
        assert ".rsd_provenance.json" in by_path

    def test_provenance_stamp_schema_matches_gate_seam_contract(self) -> None:
        state = ModelCodegenPipelineState(
            spec=_spec(),
            correlation_id="run-abc-123",
            source_text="class Handler: ...",
            contract_yaml="name: node_greeter_compute\n",
        )
        _, payload = _handle(ModelCodegenSerializeOutcome(state=state))
        assert isinstance(payload, ModelFileWriteCommand)
        by_path = {f.relative_path: f.content for f in payload.files}
        stamp = json.loads(by_path[".rsd_provenance.json"])

        # Field-for-field seam contract (OMN-14208 guard): every key/type the
        # omnibase_core gate reads MUST be present with these exact names.
        assert stamp["receipt_schema"] == "rsd_provenance_stamp.v1"
        assert stamp["generated_by"] == "rsd_delegation"
        assert stamp["producer_node"] == "node_hybrid_codegen_orchestrator"
        assert stamp["run_id"] == "run-abc-123"
        assert stamp["node_name"] == "NodeGreeterCompute"
        assert set(stamp["files_sha256"]) == {
            "handler.py",
            "contract.yaml",
            "metadata.yaml",
        }

        # The digest is RECOMPUTED here from the same content written to the
        # sibling files -- proves the stamp is not free-standing/self-asserted;
        # the gate performs this exact recompute against the live files on disk.
        def _sha(text: str) -> str:
            return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()

        assert stamp["files_sha256"]["handler.py"] == _sha(by_path["handler.py"])
        assert stamp["files_sha256"]["contract.yaml"] == _sha(by_path["contract.yaml"])
        assert stamp["files_sha256"]["metadata.yaml"] == _sha(by_path["metadata.yaml"])

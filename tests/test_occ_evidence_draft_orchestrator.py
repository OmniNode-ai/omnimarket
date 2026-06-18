"""TDD tests for the OCC evidence draft orchestrator (OMN-12580, Phase 5).

The orchestrator delegates model work over the live delegation surface
(``onex.cmd.omnibase-infra.delegation-request.v1``) and never calls a model
directly. The drafts it produces are always PROVISIONAL.

The end-to-end chain test proves a model-drafted OCC artifact, once validated by
``node_occ_evidence_validator_compute``, reaches the existing OCC PR writer
(completing blocker B4's chain).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)

from omnimarket.events.occ_evidence import (
    EnumEvidenceLifecycleState,
    EnumRuntimeLane,
    ModelOccEvidenceDraft,
    ModelOccEvidenceDraftRequest,
    ModelRuntimeDeploymentProof,
)
from omnimarket.nodes.evidence_pipeline_native import coerce_validation, write_occ_pr
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_occ_evidence_draft_orchestrator.handlers.handler_occ_evidence_draft_orchestrator import (
    HandlerOccEvidenceDraftOrchestrator,
)
from omnimarket.nodes.node_occ_evidence_draft_orchestrator.models.model_occ_evidence_draft_failed import (
    EnumOccDraftFailureReason,
    ModelOccEvidenceDraftFailed,
)
from omnimarket.nodes.node_occ_evidence_validator_compute.handlers.handler_occ_evidence_validator import (
    HandlerOccEvidenceValidator,
)
from omnimarket.nodes.node_occ_evidence_validator_compute.models.model_occ_evidence_validate_command import (
    ModelOccEvidenceValidateCommand,
)

CORRELATION_ID = UUID("11111111-1111-1111-1111-111111111111")
DEPLOYMENT_ID = UUID("22222222-2222-2222-2222-222222222222")
TICKET_ID = "OMN-12580"
REPOSITORY = "OmniNode-ai/omnimarket"
SOURCE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
LANE = EnumRuntimeLane.STABILITY_TEST
REQUESTED_AT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
RECEIPT_CMD = "uv run pytest tests/test_redeploy_fsm.py -k deploy -v"


def _proof() -> ModelRuntimeDeploymentProof:
    return ModelRuntimeDeploymentProof(
        correlation_id=CORRELATION_ID,
        deployment_id=DEPLOYMENT_ID,
        runtime_lane=LANE,
        source_sha=SOURCE_SHA,
        image_digest=IMAGE_DIGEST,
        compose_project="omnibase-infra-stability-test",
        health_status="pass",
        ready_status="pass",
        probed_at=REQUESTED_AT,
        status="success",
        topology_manifest_sha256="c" * 64,
    )


def _request() -> ModelOccEvidenceDraftRequest:
    return ModelOccEvidenceDraftRequest(
        correlation_id=CORRELATION_ID,
        deployment_id=DEPLOYMENT_ID,
        ticket_id=TICKET_ID,
        runtime_lane=LANE,
        target_occ_repo="OmniNode-ai/onex_change_control",
        model_profile="local:qwen2.5-coder",
        requested_at=REQUESTED_AT,
        runtime_deployment_proof=_proof(),
        required_receipts=(RECEIPT_CMD,),
    )


def _model_response_content() -> str:
    contract_yaml = (
        f"ticket_id: {TICKET_ID}\n"
        f"repository: {REPOSITORY}\n"
        f"source_sha: {SOURCE_SHA}\n"
        f"image_digest: {IMAGE_DIGEST}\n"
        f"runtime_lane: {LANE.value}\n"
        "intent: Phase 5 OCC evidence drafting + deterministic validation.\n"
    )
    receipt_yaml = (
        "verifier: omnimarket.node_occ_evidence_validator_compute\n"
        f"probe_command: {RECEIPT_CMD}\n"
        "status: PASS\n"
    )
    return json.dumps(
        {
            "contract_yaml": contract_yaml,
            "pr_body": "Phase 5 OCC evidence draft body.",
            "receipt_yamls": [receipt_yaml],
        }
    )


def _inference_response(content: str, *, error: str = "") -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=str(CORRELATION_ID),
        content=content,
        model_used="local:qwen2.5-coder",
        error_message=error,
    )


@pytest.mark.unit
def test_build_delegation_request_uses_delegation_surface() -> None:
    handler = HandlerOccEvidenceDraftOrchestrator()
    command = handler.build_delegation_request(_request())

    assert isinstance(command, ModelDelegationRequest)
    assert command.task_type == "code_generation"
    assert command.correlation_id == CORRELATION_ID
    # The prompt must carry the authoritative pins the validator later checks.
    assert SOURCE_SHA in command.prompt
    assert IMAGE_DIGEST in command.prompt
    assert TICKET_ID in command.prompt


@pytest.mark.unit
def test_contract_does_not_subscribe_to_delegation_lifecycle_topics() -> None:
    contract_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_occ_evidence_draft_orchestrator"
        / "contract.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    subscribe_topics = set(contract["event_bus"]["subscribe_topics"])

    assert subscribe_topics == {"onex.cmd.omnimarket.occ-evidence-draft-start.v1"}
    assert "onex.evt.omnibase-infra.inference-response.v1" not in subscribe_topics
    assert "onex.evt.omnibase-infra.routing-decision.v1" not in subscribe_topics
    assert "onex.evt.omnibase-infra.quality-gate-result.v1" not in subscribe_topics


@pytest.mark.unit
def test_materialize_draft_is_provisional() -> None:
    handler = HandlerOccEvidenceDraftOrchestrator()
    draft = handler.materialize_draft(
        _request(), _inference_response(_model_response_content())
    )

    assert isinstance(draft, ModelOccEvidenceDraft)
    assert draft.evidence_lifecycle_state is EnumEvidenceLifecycleState.PROVISIONAL
    assert draft.ticket_id == TICKET_ID
    assert draft.model_identity == "local:qwen2.5-coder"
    assert len(draft.receipt_yamls) == 1


@pytest.mark.unit
def test_delegation_error_yields_failed_event() -> None:
    handler = HandlerOccEvidenceDraftOrchestrator()
    outcome = handler.materialize_draft(
        _request(), _inference_response("", error="model lane unavailable")
    )

    assert isinstance(outcome, ModelOccEvidenceDraftFailed)
    assert outcome.failure_reason is EnumOccDraftFailureReason.DELEGATION_ERROR


@pytest.mark.unit
def test_unparseable_response_yields_failed_event() -> None:
    handler = HandlerOccEvidenceDraftOrchestrator()
    outcome = handler.materialize_draft(
        _request(), _inference_response("not json at all")
    )

    assert isinstance(outcome, ModelOccEvidenceDraftFailed)
    assert outcome.failure_reason is EnumOccDraftFailureReason.UNPARSEABLE_RESPONSE


@pytest.mark.unit
def test_missing_artifacts_yields_failed_event() -> None:
    handler = HandlerOccEvidenceDraftOrchestrator()
    outcome = handler.materialize_draft(
        _request(), _inference_response(json.dumps({"pr_body": "only a body"}))
    )

    assert isinstance(outcome, ModelOccEvidenceDraftFailed)
    assert outcome.failure_reason is EnumOccDraftFailureReason.MISSING_ARTIFACTS


@pytest.mark.unit
def test_draft_orchestrator_to_validator_to_occ_pr_writer_chain() -> None:
    """Full Phase 5 chain: draft -> validate -> OCC PR writer (blocker B4)."""
    draft_handler = HandlerOccEvidenceDraftOrchestrator()
    request = _request()
    draft = draft_handler.materialize_draft(
        request, _inference_response(_model_response_content())
    )
    assert isinstance(draft, ModelOccEvidenceDraft)

    validator = HandlerOccEvidenceValidator()
    validate_command = ModelOccEvidenceValidateCommand(
        correlation_id=CORRELATION_ID,
        deployment_id=DEPLOYMENT_ID,
        draft=draft,
        runtime_deployment_proof=_proof(),
        runtime_lane=LANE,
        expected_repository=REPOSITORY,
        expected_source_sha=SOURCE_SHA,
        expected_image_digest=IMAGE_DIGEST,
        validated_at=datetime(2026, 6, 1, 12, 5, 0, tzinfo=UTC),
        required_receipt_commands=(RECEIPT_CMD,),
        allowed_receipt_commands=(RECEIPT_CMD,),
    )
    validation = validator.handle(validate_command)
    assert isinstance(validation, ModelEvidenceValidationResult)
    assert validation.validation_state == "PASSED"

    pr = write_occ_pr(coerce_validation(validation))
    assert pr.ticket_id == TICKET_ID
    assert pr.occ_repository.endswith("onex_change_control")

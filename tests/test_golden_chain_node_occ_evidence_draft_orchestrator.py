"""Golden-chain coverage for node_occ_evidence_draft_orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime

from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)

from omnimarket.events.occ_evidence import (
    ModelOccEvidenceDraft,
)
from omnimarket.nodes.evidence_pipeline_native import coerce_validation, write_occ_pr
from omnimarket.nodes.node_occ_evidence_validator_compute.handlers.handler_occ_evidence_validator import (
    HandlerOccEvidenceValidator,
)
from omnimarket.nodes.node_occ_evidence_validator_compute.models.model_occ_evidence_validate_command import (
    ModelOccEvidenceValidateCommand,
)
from tests.test_occ_evidence_draft_orchestrator import (
    DEPLOYMENT_ID,
    IMAGE_DIGEST,
    LANE,
    RECEIPT_CMD,
    REPOSITORY,
    SOURCE_SHA,
    TICKET_ID,
    _inference_response,
    _model_response_content,
    _proof,
    _request,
)


def test_golden_chain_draft_to_validator_to_occ_pr_writer() -> None:
    from omnimarket.nodes.node_occ_evidence_draft_orchestrator.handlers.handler_occ_evidence_draft_orchestrator import (
        HandlerOccEvidenceDraftOrchestrator,
    )

    request = _request()
    draft = HandlerOccEvidenceDraftOrchestrator().materialize_draft(
        request, _inference_response(_model_response_content())
    )

    assert isinstance(draft, ModelOccEvidenceDraft)

    validation = HandlerOccEvidenceValidator().handle(
        ModelOccEvidenceValidateCommand(
            correlation_id=request.correlation_id,
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
    )

    assert isinstance(validation, ModelEvidenceValidationResult)
    assert validation.validation_state == "PASSED"

    pr = write_occ_pr(coerce_validation(validation))
    assert pr.ticket_id == TICKET_ID
    assert pr.occ_repository.endswith("onex_change_control")

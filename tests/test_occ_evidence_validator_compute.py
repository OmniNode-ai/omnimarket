"""TDD receipt tests for the deterministic OCC evidence validator (OMN-12580).

Receipt tests (from the design doc "Receipt tests" list):
- Generated OCC draft with wrong SHA is rejected.
- Generated OCC draft with missing probe command is rejected.
- Valid draft passes deterministic validation and reaches the OCC PR writer.

The PASS path must emit the EXISTING compat ``ModelEvidenceValidationResult`` so
``node_occ_pr_writer_effect`` (which coerces and writes the OCC PR) fires.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID

import pytest
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)

from omnimarket.nodes.evidence_pipeline_native import coerce_validation, write_occ_pr
from omnimarket.nodes.node_occ_evidence_validator_compute.handlers.handler_occ_evidence_validator import (
    HandlerOccEvidenceValidator,
)
from omnimarket.nodes.node_occ_evidence_validator_compute.models.model_occ_evidence_validate_command import (
    ModelOccEvidenceValidateCommand,
)
from omnimarket.nodes.node_redeploy.models.model_occ_evidence_draft import (
    EnumEvidenceLifecycleState,
    ModelOccEvidenceDraft,
    ModelOccEvidenceDraftValidationResult,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
)
from omnimarket.nodes.node_redeploy.models.model_runtime_deployment import (
    ModelRuntimeDeploymentProof,
)

CORRELATION_ID = UUID("11111111-1111-1111-1111-111111111111")
DEPLOYMENT_ID = UUID("22222222-2222-2222-2222-222222222222")
TICKET_ID = "OMN-12580"
REPOSITORY = "OmniNode-ai/omnimarket"
SOURCE_SHA = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
LANE = EnumRuntimeLane.STABILITY_TEST
PROBED_AT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
VALIDATED_AT = datetime(2026, 6, 1, 12, 5, 0, tzinfo=UTC)
RECEIPT_CMD = "uv run pytest tests/test_redeploy_fsm.py -k deploy -v"


def _contract_yaml(
    *,
    source_sha: str = SOURCE_SHA,
    image_digest: str = IMAGE_DIGEST,
    repository: str = REPOSITORY,
    lane: str = LANE.value,
) -> str:
    return (
        f"ticket_id: {TICKET_ID}\n"
        f"repository: {repository}\n"
        f"source_sha: {source_sha}\n"
        f"image_digest: {image_digest}\n"
        f"runtime_lane: {lane}\n"
        "intent: Phase 5 OCC evidence drafting + deterministic validation.\n"
    )


def _receipt_yaml(command: str = RECEIPT_CMD) -> str:
    return (
        "verifier: omnimarket.node_occ_evidence_validator_compute\n"
        f"probe_command: {command}\n"
        "status: PASS\n"
    )


def _draft_hash(
    ticket: str, contract: str, pr_body: str, receipts: tuple[str, ...]
) -> str:
    payload = "\n".join((ticket, contract, pr_body, *receipts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _draft(
    *,
    contract_yaml: str | None = None,
    receipt_yamls: tuple[str, ...] = (_receipt_yaml(),),
    pr_body: str = "Phase 5 OCC evidence draft body.",
) -> ModelOccEvidenceDraft:
    contract = contract_yaml if contract_yaml is not None else _contract_yaml()
    return ModelOccEvidenceDraft(
        correlation_id=CORRELATION_ID,
        deployment_id=DEPLOYMENT_ID,
        ticket_id=TICKET_ID,
        draft_hash=_draft_hash(TICKET_ID, contract, pr_body, receipt_yamls),
        contract_yaml=contract,
        pr_body=pr_body,
        model_identity="local:qwen2.5-coder",
        generated_at=PROBED_AT,
        receipt_yamls=receipt_yamls,
        evidence_lifecycle_state=EnumEvidenceLifecycleState.PROVISIONAL,
    )


def _proof(
    *, source_sha: str = SOURCE_SHA, image_digest: str = IMAGE_DIGEST
) -> ModelRuntimeDeploymentProof:
    return ModelRuntimeDeploymentProof(
        correlation_id=CORRELATION_ID,
        deployment_id=DEPLOYMENT_ID,
        runtime_lane=LANE,
        source_sha=source_sha,
        image_digest=image_digest,
        compose_project="omnibase-infra-stability-test",
        health_status="pass",
        ready_status="pass",
        probed_at=PROBED_AT,
        status="success",
        topology_manifest_sha256="c" * 64,
    )


def _command(
    *,
    draft: ModelOccEvidenceDraft | None = None,
    proof: ModelRuntimeDeploymentProof | None = None,
    expected_source_sha: str = SOURCE_SHA,
    expected_image_digest: str = IMAGE_DIGEST,
    required_receipt_commands: tuple[str, ...] = (RECEIPT_CMD,),
    allowed_receipt_commands: tuple[str, ...] = (RECEIPT_CMD,),
    receipt_gate_fixture_passes: bool = True,
) -> ModelOccEvidenceValidateCommand:
    return ModelOccEvidenceValidateCommand(
        correlation_id=CORRELATION_ID,
        deployment_id=DEPLOYMENT_ID,
        draft=draft if draft is not None else _draft(),
        runtime_deployment_proof=proof if proof is not None else _proof(),
        runtime_lane=LANE,
        expected_repository=REPOSITORY,
        expected_source_sha=expected_source_sha,
        expected_image_digest=expected_image_digest,
        validated_at=VALIDATED_AT,
        required_receipt_commands=required_receipt_commands,
        allowed_receipt_commands=allowed_receipt_commands,
        receipt_gate_fixture_passes=receipt_gate_fixture_passes,
    )


@pytest.mark.unit
def test_valid_draft_passes_and_reaches_occ_pr_writer() -> None:
    handler = HandlerOccEvidenceValidator()
    result = handler.handle(_command())

    assert isinstance(result, ModelEvidenceValidationResult)
    assert result.validation_state == "PASSED"
    assert result.evidence_lifecycle_state == "VALIDATED"
    assert result.ticket_id == TICKET_ID
    assert result.repository == REPOSITORY
    assert result.verifier_identity == "omnimarket.node_occ_evidence_validator_compute"

    # The PASS model must drive the existing OCC PR writer path end-to-end.
    coerced = coerce_validation(result)
    pr = write_occ_pr(coerced)
    assert pr.ticket_id == TICKET_ID
    assert pr.occ_repository.endswith("onex_change_control")


@pytest.mark.unit
def test_wrong_sha_draft_is_rejected() -> None:
    handler = HandlerOccEvidenceValidator()
    wrong = _draft(contract_yaml=_contract_yaml(source_sha="f" * 40))
    result = handler.handle(_command(draft=wrong))

    assert isinstance(result, ModelOccEvidenceDraftValidationResult)
    assert result.validation_state == "FAILED"
    assert result.sha_match_status == "fail"
    assert "source_sha_mismatch" in result.blocking_reason_codes


@pytest.mark.unit
def test_missing_probe_command_draft_is_rejected() -> None:
    handler = HandlerOccEvidenceValidator()
    # Receipt YAML with no probe_command key.
    bad_receipt = (
        "verifier: omnimarket.node_occ_evidence_validator_compute\nstatus: PASS\n"
    )
    bad = _draft(receipt_yamls=(bad_receipt,))
    result = handler.handle(_command(draft=bad))

    assert isinstance(result, ModelOccEvidenceDraftValidationResult)
    assert result.validation_state == "FAILED"
    assert result.receipt_probe_status == "fail"
    assert "receipt_probe_command_missing" in result.blocking_reason_codes


@pytest.mark.unit
def test_image_digest_mismatch_is_rejected() -> None:
    handler = HandlerOccEvidenceValidator()
    wrong = _draft(contract_yaml=_contract_yaml(image_digest="sha256:" + "d" * 64))
    result = handler.handle(_command(draft=wrong))

    assert isinstance(result, ModelOccEvidenceDraftValidationResult)
    assert result.image_digest_match_status == "fail"
    assert "image_digest_mismatch" in result.blocking_reason_codes


@pytest.mark.unit
def test_disallowed_receipt_command_is_rejected() -> None:
    handler = HandlerOccEvidenceValidator()
    result = handler.handle(_command(allowed_receipt_commands=("rm -rf /",)))

    assert isinstance(result, ModelOccEvidenceDraftValidationResult)
    assert result.receipt_probe_status == "fail"
    assert "receipt_command_not_allowed" in result.blocking_reason_codes


@pytest.mark.unit
def test_stale_topology_proof_is_rejected() -> None:
    handler = HandlerOccEvidenceValidator()
    old_proof = ModelRuntimeDeploymentProof(
        correlation_id=CORRELATION_ID,
        deployment_id=DEPLOYMENT_ID,
        runtime_lane=LANE,
        source_sha=SOURCE_SHA,
        image_digest=IMAGE_DIGEST,
        compose_project="omnibase-infra-stability-test",
        health_status="pass",
        ready_status="pass",
        probed_at=datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC),
        status="success",
        topology_manifest_sha256="c" * 64,
    )
    result = handler.handle(_command(proof=old_proof))

    assert isinstance(result, ModelOccEvidenceDraftValidationResult)
    assert result.topology_freshness_status == "stale"
    assert "topology_proof_stale" in result.blocking_reason_codes


@pytest.mark.unit
def test_malformed_contract_yaml_is_rejected() -> None:
    handler = HandlerOccEvidenceValidator()
    bad = _draft(contract_yaml="ticket_id: [unterminated\n")
    result = handler.handle(_command(draft=bad))

    assert isinstance(result, ModelOccEvidenceDraftValidationResult)
    assert result.schema_status == "fail"
    assert "schema_invalid" in result.blocking_reason_codes


@pytest.mark.unit
def test_validation_is_deterministic_under_repeat() -> None:
    handler = HandlerOccEvidenceValidator()
    command = _command()
    first = handler.handle(command)
    second = handler.handle(command)
    assert isinstance(first, ModelEvidenceValidationResult)
    assert isinstance(second, ModelEvidenceValidationResult)
    assert first.model_dump() == second.model_dump()

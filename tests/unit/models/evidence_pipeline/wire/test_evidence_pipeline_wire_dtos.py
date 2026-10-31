# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

import pytest
from pydantic import ValidationError

from omnimarket.models.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)
from omnimarket.models.evidence_pipeline.wire.model_evidence_bundle import (
    ModelEvidenceBundle,
)
from omnimarket.models.evidence_pipeline.wire.model_evidence_pipeline_command import (
    ModelEvidencePipelineCommand,
)
from omnimarket.models.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)
from omnimarket.models.evidence_pipeline.wire.model_gap_report import ModelGapReport
from omnimarket.models.evidence_pipeline.wire.model_occ_pr_reference import (
    ModelOccPrReference,
)
from omnimarket.models.evidence_pipeline.wire.model_raw_evidence_payload import (
    ModelRawEvidencePayload,
)


@pytest.mark.unit
def test_evidence_pipeline_command_round_trips() -> None:
    command = ModelEvidencePipelineCommand(
        correlation_id="corr-001",
        validation_run_id="run-001",
        ticket_id="OMN-11443",
        repository="omnimarket",
        source_commit_sha="abcdef1",
        requested_at="2026-05-21T20:00:00Z",
        trigger_surface="pull_request_merge",
        source_pr=12,
        topology_affecting=True,
    )

    restored = ModelEvidencePipelineCommand.model_validate_json(
        command.model_dump_json()
    )
    assert restored == command
    assert restored.schema_version == "1.0.0"


@pytest.mark.unit
def test_raw_evidence_payload_declares_collected_surfaces() -> None:
    payload = ModelRawEvidencePayload(
        correlation_id="corr-001",
        validation_run_id="run-001",
        ticket_id="OMN-11443",
        repository="omnimarket",
        source_commit_sha="abcdef1",
        collected_at="2026-05-21T20:01:00Z",
        collector_identity="node_evidence_collector_effect",
        source_surfaces=("github_pr", "ci_artifacts"),
        source_pr=12,
        changed_files=("src/node.py",),
    )

    assert payload.source_surfaces == ("github_pr", "ci_artifacts")
    assert payload.changed_files == ("src/node.py",)


@pytest.mark.unit
def test_evidence_bundle_carries_provenance_hash_and_validator_version() -> None:
    bundle = ModelEvidenceBundle(
        correlation_id="corr-001",
        validation_run_id="run-001",
        ticket_id="OMN-11443",
        repository="omnimarket",
        source_surfaces=("github_pr", "ci_artifacts"),
        source_commit_sha="abcdef1",
        evidence_bundle_hash="sha256:bundle",
        validator_version="evidence-extractor.v1",
        extracted_at="2026-05-21T20:02:00Z",
        source_pr=12,
        source_ci_run="ci-123",
        source_projection_refs=("projection://deployments/dep-001",),
    )

    assert bundle.evidence_bundle_hash == "sha256:bundle"
    assert bundle.source_projection_refs == ("projection://deployments/dep-001",)


@pytest.mark.unit
def test_validation_result_has_identity_and_lifecycle_state() -> None:
    result = ModelEvidenceValidationResult(
        correlation_id="corr-001",
        validation_run_id="run-001",
        ticket_id="OMN-11443",
        repository="omnimarket",
        contract_hash="sha256:contract",
        evidence_bundle_hash="sha256:bundle",
        verifier_identity="contract_matcher_compute.v1",
        validator_version="contract-matcher.v1",
        validated_at="2026-05-21T20:03:00Z",
        validation_state="PASSED",
        evidence_lifecycle_state="VALIDATED",
        topology_affecting=True,
        requirement_results={"dod.contract": "passed"},
    )

    assert result.validation_state == "PASSED"
    assert result.evidence_lifecycle_state == "VALIDATED"
    assert result.correlation_id == "corr-001"
    assert result.validation_run_id == "run-001"


@pytest.mark.unit
def test_gap_report_accepts_all_declared_gap_classifications() -> None:
    report = ModelGapReport(
        correlation_id="corr-001",
        validation_run_id="run-001",
        deployment_id="deployment-001",
        generated_at="2026-05-21T20:04:00Z",
        validator_version="gap-analyzer.v1",
        gap_classifications={
            "ticket:OMN-1": "MISSING",
            "ticket:OMN-2": "STALE",
            "ticket:OMN-3": "SUPERSEDED",
            "ticket:OMN-4": "HASH_MISMATCH",
            "ticket:OMN-5": "RECEIPT_MISSING",
            "ticket:OMN-6": "VALIDATION_FAILED",
            "ticket:OMN-7": "UNKNOWN",
        },
    )

    assert set(report.gap_classifications.values()) == {
        "MISSING",
        "STALE",
        "SUPERSEDED",
        "HASH_MISMATCH",
        "RECEIPT_MISSING",
        "VALIDATION_FAILED",
        "UNKNOWN",
    }


@pytest.mark.unit
def test_readiness_result_carries_machine_readable_blockers() -> None:
    result = ModelDeploymentReadinessResult(
        correlation_id="corr-001",
        validation_run_id="run-001",
        deployment_id="deployment-001",
        readiness_state="BLOCKED",
        scored_at="2026-05-21T20:05:00Z",
        validator_version="readiness-scorer.v1",
        gap_report_hash="sha256:gap",
        blocking_reason_codes=("RECEIPT_MISSING",),
        required_evidence_refs=("evidence://OMN-11443",),
        missing_contracts=("OMN-11443",),
        topology_affecting=True,
        topology_metadata={"runtime_manifest": "sha256:manifest"},
    )

    assert result.readiness_state == "BLOCKED"
    assert result.blocking_reason_codes == ("RECEIPT_MISSING",)
    assert result.topology_metadata["runtime_manifest"] == "sha256:manifest"


@pytest.mark.unit
def test_occ_pr_reference_defaults_to_provisional_lifecycle() -> None:
    reference = ModelOccPrReference(
        correlation_id="corr-001",
        validation_run_id="run-001",
        ticket_id="OMN-11443",
        occ_repository="onex_change_control",
        pr_number=123,
        pr_url="https://github.com/OmniNode-ai/onex_change_control/pull/123",
        branch="jonah/omn-11443-evidence",
        created_at="2026-05-21T20:06:00Z",
        writer_identity="node_occ_pr_writer_effect",
    )

    assert reference.evidence_lifecycle_state == "PROVISIONAL"


@pytest.mark.unit
def test_models_are_frozen_and_reject_extra_fields() -> None:
    command = ModelEvidencePipelineCommand(
        correlation_id="corr-001",
        validation_run_id="run-001",
        ticket_id="OMN-11443",
        repository="omnimarket",
        source_commit_sha="abcdef1",
        requested_at="2026-05-21T20:00:00Z",
        trigger_surface="manual",
    )

    with pytest.raises(ValidationError):
        command.ticket_id = "OMN-1"

    with pytest.raises(ValidationError):
        ModelOccPrReference(
            correlation_id="corr-001",
            validation_run_id="run-001",
            ticket_id="OMN-11443",
            occ_repository="onex_change_control",
            pr_number=123,
            pr_url="https://github.com/OmniNode-ai/onex_change_control/pull/123",
            branch="jonah/omn-11443-evidence",
            created_at="2026-05-21T20:06:00Z",
            writer_identity="node_occ_pr_writer_effect",
            unexpected="nope",
        )


@pytest.mark.unit
def test_invalid_literal_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelDeploymentReadinessResult(
            correlation_id="corr-001",
            validation_run_id="run-001",
            deployment_id="deployment-001",
            readiness_state="DONE",
            scored_at="2026-05-21T20:05:00Z",
            validator_version="readiness-scorer.v1",
            gap_report_hash="sha256:gap",
        )

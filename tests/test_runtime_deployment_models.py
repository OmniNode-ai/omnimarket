# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model foundation tests for node-based runtime deployment (OMN-12576).

Covers the lane/digest/promotion evolution of ``ModelRedeployCommand`` and the
repo-private proof / OCC-draft / draft-validation models that feed phases 2-6 of
the deployment design (plan
``docs/plans/2026-06-01-node-based-runtime-deployment-occ-tdd.md``).

The PASS path of OCC draft validation reuses the existing
``omnibase_compat ... ModelEvidenceValidationResult`` on
``onex.evt.omnimarket.evidence-validated.v1`` (blocker B4);
``ModelOccEvidenceDraftValidationResult`` is the INTERNAL reject/audit shape only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.events.occ_evidence import (
    EnumEvidenceLifecycleState,
    ModelOccEvidenceDraft,
    ModelOccEvidenceDraftRequest,
    ModelOccEvidenceDraftValidationResult,
)
from omnimarket.nodes.node_redeploy.models.model_deploy_agent_events import (
    EnumBuildSource,
    ModelDeployRebuildCommand,
)
from omnimarket.nodes.node_redeploy.models.model_redeploy_command import (
    EnumRuntimeLane,
    ModelRedeployCommand,
)
from omnimarket.nodes.node_redeploy.models.model_runtime_deployment import (
    ModelRuntimeBuildResult,
    ModelRuntimeDeploymentProof,
    ModelRuntimeDeploymentRequest,
    ModelRuntimeDeployResult,
)


@pytest.mark.unit
def test_runtime_lane_enum_values() -> None:
    assert EnumRuntimeLane.DEV.value == "dev"
    assert EnumRuntimeLane.STABILITY_TEST.value == "stability-test"
    assert EnumRuntimeLane.PROD.value == "prod"


@pytest.mark.unit
def test_redeploy_command_round_trips_new_fields() -> None:
    """ModelRedeployCommand round-trips the lane/digest/promotion fields."""
    command = ModelRedeployCommand(
        correlation_id=uuid4(),
        requested_at=datetime.now(tz=UTC),
        runtime_lane=EnumRuntimeLane.PROD,
        image_ref="ghcr.io/omninode/omninode-runtime:main",
        image_digest="sha256:deadbeef",
        promotion_batch_id="promo-2026-06-01-001",
    )

    dumped = command.model_dump(mode="json")
    restored = ModelRedeployCommand.model_validate(dumped)

    assert restored == command
    assert restored.runtime_lane is EnumRuntimeLane.PROD
    assert restored.image_digest == "sha256:deadbeef"
    assert restored.promotion_batch_id == "promo-2026-06-01-001"


@pytest.mark.unit
def test_redeploy_command_defaults_preserve_existing_callers() -> None:
    """The new fields default to None/dev so existing construction stays valid."""
    command = ModelRedeployCommand(
        correlation_id=uuid4(),
        requested_at=datetime.now(tz=UTC),
    )

    assert command.runtime_lane is EnumRuntimeLane.DEV
    assert command.image_digest is None
    assert command.promotion_batch_id is None


@pytest.mark.unit
def test_redeploy_command_rejects_unknown_lane() -> None:
    with pytest.raises(ValidationError):
        ModelRedeployCommand(
            correlation_id=uuid4(),
            requested_at=datetime.now(tz=UTC),
            runtime_lane="canary",  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_redeploy_command_requires_prod_pins() -> None:
    """Prod redeploys must pin the stability-test READY artifact."""
    with pytest.raises(ValidationError, match="image_digest and promotion_batch_id"):
        ModelRedeployCommand(
            correlation_id=uuid4(),
            requested_at=datetime.now(tz=UTC),
            runtime_lane=EnumRuntimeLane.PROD,
            image_digest="sha256:deadbeef",
        )


@pytest.mark.unit
def test_deploy_agent_command_round_trips_lane_payload() -> None:
    command = ModelDeployRebuildCommand(
        correlation_id=str(uuid4()),
        requested_by="node_redeploy",
        scope="runtime",
        runtime_lane=EnumRuntimeLane.STABILITY_TEST,
        build_source=EnumBuildSource.WORKSPACE,
        services=["omninode-runtime"],
        git_ref="origin/dev",
        image_ref="ghcr.io/omninode/omninode-runtime:dev",
        image_digest="sha256:" + "a" * 64,
    )

    dumped = command.model_dump(mode="json")
    restored = ModelDeployRebuildCommand.model_validate(dumped)

    assert restored.runtime_lane is EnumRuntimeLane.STABILITY_TEST
    assert restored.build_source is EnumBuildSource.WORKSPACE
    assert dumped["runtime_lane"] == "stability-test"
    assert dumped["build_source"] == "workspace"
    assert dumped["image_digest"] == "sha256:" + "a" * 64


@pytest.mark.unit
def test_deploy_agent_command_requires_runtime_lane() -> None:
    with pytest.raises(ValidationError, match="runtime_lane"):
        ModelDeployRebuildCommand(
            correlation_id=str(uuid4()),
            requested_by="node_redeploy",
            scope="runtime",
            git_ref="origin/dev",
        )


@pytest.mark.unit
def test_deploy_agent_command_requires_prod_digest() -> None:
    with pytest.raises(ValidationError, match="image_digest"):
        ModelDeployRebuildCommand(
            correlation_id=str(uuid4()),
            requested_by="node_redeploy",
            scope="runtime",
            runtime_lane=EnumRuntimeLane.PROD,
            git_ref="origin/main",
        )


def _deployment_request_kwargs() -> dict[str, object]:
    return {
        "correlation_id": uuid4(),
        "deployment_id": uuid4(),
        "runtime_lane": EnumRuntimeLane.STABILITY_TEST,
        "source_branch": "main",
        "source_sha": "abc123",
        "requested_by": "runtime-rebuild-trigger",
        "requested_at": datetime.now(tz=UTC),
    }


@pytest.mark.unit
def test_deployment_request_rejects_missing_source_sha() -> None:
    kwargs = _deployment_request_kwargs()
    del kwargs["source_sha"]
    with pytest.raises(ValidationError):
        ModelRuntimeDeploymentRequest(**kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_deployment_request_rejects_missing_runtime_lane() -> None:
    kwargs = _deployment_request_kwargs()
    del kwargs["runtime_lane"]
    with pytest.raises(ValidationError):
        ModelRuntimeDeploymentRequest(**kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_deployment_request_requires_prod_pins() -> None:
    kwargs = _deployment_request_kwargs()
    kwargs["runtime_lane"] = EnumRuntimeLane.PROD
    kwargs["promotion_batch_id"] = "promo-2026-06-01-001"
    with pytest.raises(ValidationError, match="image_digest and promotion_batch_id"):
        ModelRuntimeDeploymentRequest(**kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_build_result_rejects_missing_image_digest() -> None:
    with pytest.raises(ValidationError):
        ModelRuntimeBuildResult(
            correlation_id=uuid4(),
            deployment_id=uuid4(),
            runtime_lane=EnumRuntimeLane.STABILITY_TEST,
            source_sha="abc123",
            # image_digest missing
            image_ref="ghcr.io/omninode/omninode-runtime:main",
            build_source="dev",
            build_started_at=datetime.now(tz=UTC),
            build_completed_at=datetime.now(tz=UTC),
            status="success",  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_deploy_result_requires_lane_and_digest() -> None:
    result = ModelRuntimeDeployResult(
        correlation_id=uuid4(),
        deployment_id=uuid4(),
        runtime_lane=EnumRuntimeLane.PROD,
        source_sha="abc123",
        image_digest="sha256:deadbeef",
        compose_project="omnibase-infra-prod",
        compose_files=("docker-compose.infra.yml", "docker-compose.prod.yml"),
        services_restarted=("omninode-runtime",),
        deploy_started_at=datetime.now(tz=UTC),
        deploy_completed_at=datetime.now(tz=UTC),
        status="success",
    )
    assert result.runtime_lane is EnumRuntimeLane.PROD
    assert result.rollback_target is None


@pytest.mark.unit
def test_deployment_proof_requires_image_digest() -> None:
    """image_digest is the prod-gate authority — a proof without it is invalid."""
    with pytest.raises(ValidationError):
        ModelRuntimeDeploymentProof(
            correlation_id=uuid4(),
            deployment_id=uuid4(),
            runtime_lane=EnumRuntimeLane.STABILITY_TEST,
            source_sha="abc123",
            # image_digest missing
            compose_project="omnibase-infra-stability-test",
            health_status="pass",
            ready_status="pass",
            probed_at=datetime.now(tz=UTC),
            status="success",  # type: ignore[arg-type]
        )


def _draft_request_kwargs() -> dict[str, object]:
    return {
        "correlation_id": uuid4(),
        "deployment_id": uuid4(),
        "ticket_id": "OMN-12574",
        "runtime_lane": EnumRuntimeLane.STABILITY_TEST,
        "target_occ_repo": "onex_change_control",
        "model_profile": "local-occ-draft",
        "requested_at": datetime.now(tz=UTC),
    }


@pytest.mark.unit
def test_occ_evidence_draft_request_rejects_missing_ticket_id() -> None:
    kwargs = _draft_request_kwargs()
    del kwargs["ticket_id"]
    with pytest.raises(ValidationError):
        ModelOccEvidenceDraftRequest(**kwargs)  # type: ignore[arg-type]


@pytest.mark.unit
def test_occ_evidence_draft_is_always_provisional() -> None:
    """A model-generated draft is provisional and cannot be marked authoritative."""
    draft = ModelOccEvidenceDraft(
        correlation_id=uuid4(),
        deployment_id=uuid4(),
        ticket_id="OMN-12574",
        draft_hash="sha256:draft",
        contract_yaml="ticket_id: OMN-12574\n",
        pr_body="draft PR body",
        model_identity="local-occ-draft@.201",
        generated_at=datetime.now(tz=UTC),
    )

    assert draft.evidence_lifecycle_state is EnumEvidenceLifecycleState.PROVISIONAL
    # The lifecycle state is fixed to PROVISIONAL and cannot be overridden.
    with pytest.raises(ValidationError):
        ModelOccEvidenceDraft(
            correlation_id=uuid4(),
            deployment_id=uuid4(),
            ticket_id="OMN-12574",
            draft_hash="sha256:draft",
            contract_yaml="ticket_id: OMN-12574\n",
            pr_body="draft PR body",
            model_identity="local-occ-draft@.201",
            generated_at=datetime.now(tz=UTC),
            evidence_lifecycle_state=EnumEvidenceLifecycleState.VALIDATED,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_occ_draft_validation_result_is_stable_under_repeated_validation() -> None:
    """Re-validating the same draft yields an identical (deterministic) result."""
    common = {
        "correlation_id": uuid4(),
        "deployment_id": uuid4(),
        "ticket_id": "OMN-12574",
        "draft_hash": "sha256:draft",
        "validation_state": "FAILED",
        "blocking_reason_codes": ("SHA_MISMATCH",),
        "schema_status": "pass",
        "sha_match_status": "fail",
        "image_digest_match_status": "pass",
        "receipt_probe_status": "pass",
        "topology_freshness_status": "current",
        "validated_at": "2026-06-01T00:00:00Z",
    }
    first = ModelOccEvidenceDraftValidationResult(**common)  # type: ignore[arg-type]
    second = ModelOccEvidenceDraftValidationResult(**common)  # type: ignore[arg-type]

    assert first == second
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.validated_at == datetime(2026, 6, 1, tzinfo=UTC)


@pytest.mark.unit
def test_occ_draft_validation_result_rejects_invalid_timestamp() -> None:
    with pytest.raises(ValidationError):
        ModelOccEvidenceDraftValidationResult(
            correlation_id=uuid4(),
            deployment_id=uuid4(),
            ticket_id="OMN-12574",
            draft_hash="sha256:draft",
            validation_state="FAILED",
            schema_status="pass",
            sha_match_status="fail",
            image_digest_match_status="pass",
            receipt_probe_status="pass",
            topology_freshness_status="current",
            validated_at="not-a-timestamp",
        )

# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Phase 4 wiring tests (OMN-12579).

These tests assert the two Phase 4 deliverables:

1. ``evidence-collected -> evidence-extracted`` collection is driven *from a
   deployment proof* (``ModelRuntimeDeploymentProof``), not just from ad-hoc
   pipeline-command metadata.
2. ``node_occ_pr_writer_effect`` consumes ``onex.evt.omnimarket.evidence-validated.v1``
   (a ``ModelEvidenceValidationResult`` event) and produces a provisional OCC PR
   reference on ``onex.evt.omnimarket.occ-pr-created.v1``.

The chain is *not* exercised end-to-end with real ``evidence-validated`` traffic
here — that event is produced by the Phase 5 validator (OMN-12580). Phase 4 only
stands up the deployment-proof collection bridge and the OCC PR writer consumer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import yaml
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_pipeline_command import (
    ModelEvidencePipelineCommand,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)

from omnimarket.events.occ_evidence import (
    EnumRuntimeLane,
    ModelRuntimeDeploymentProof,
)
from omnimarket.nodes.evidence_pipeline_native import (
    DeploymentProofEvidenceCollectorAdapter,
    collect_evidence,
    evidence_command_from_deployment_proof,
    extract_evidence,
)
from omnimarket.nodes.node_evidence_collector_effect import (
    HandlerEvidenceCollectorEffect,
)
from omnimarket.nodes.node_evidence_extractor_compute import (
    HandlerEvidenceExtractorCompute,
)
from omnimarket.nodes.node_occ_pr_writer_effect import HandlerOccPrWriterEffect

_DEPLOYMENT_ID = UUID("11111111-1111-4111-8111-111111111111")
_CORRELATION_ID = UUID("22222222-2222-4222-8222-222222222222")


def _proof() -> ModelRuntimeDeploymentProof:
    return ModelRuntimeDeploymentProof(
        correlation_id=_CORRELATION_ID,
        deployment_id=_DEPLOYMENT_ID,
        runtime_lane=EnumRuntimeLane.STABILITY_TEST,
        source_sha="abcdef1234567890abcdef1234567890abcdef12",
        image_digest="sha256:deadbeefcafefeed",
        compose_project="omnibase-infra-stability-test",
        health_status="pass",
        ready_status="pass",
        probed_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        status="success",
        promotion_batch_id="batch-2026-06-01",
        runtime_addresses=("omninode-runtime:18085", "runtime-effects:18086"),
        topology_manifest_sha256="sha256:topology",
        package_versions={"omnibase_core": "0.37.0"},
        runtime_source_hash="sha256:source",
        consumer_groups=("local.omnimarket.node_redeploy.consume.v2",),
        runtime_sweep_input_ref="sweep:run-1",
    )


def test_evidence_command_is_built_from_deployment_proof() -> None:
    proof = _proof()

    command = evidence_command_from_deployment_proof(proof, ticket_id="OMN-12579")

    assert isinstance(command, ModelEvidencePipelineCommand)
    assert command.trigger_surface == "deploy"
    assert command.ticket_id == "OMN-12579"
    assert command.repository == "omnimarket"
    assert command.source_commit_sha == proof.source_sha
    assert command.deployment_id == str(proof.deployment_id)
    assert command.correlation_id == str(proof.correlation_id)
    assert command.topology_affecting is True
    # Deployment-proof facts must be threaded into collector metadata so the
    # downstream extractor materializes them deterministically.
    assert command.metadata["image_digest"] == proof.image_digest
    assert command.metadata["runtime_lane"] == proof.runtime_lane.value
    assert command.metadata["promotion_batch_id"] == proof.promotion_batch_id
    assert command.metadata["health_status"] == proof.health_status
    assert command.metadata["ready_status"] == proof.ready_status
    assert proof.topology_manifest_sha256 is not None
    assert (
        command.metadata["topology_manifest_sha256"] == proof.topology_manifest_sha256
    )


def test_collected_to_extracted_chain_carries_deployment_proof_facts() -> None:
    proof = _proof()
    command = evidence_command_from_deployment_proof(proof, ticket_id="OMN-12579")

    adapter = DeploymentProofEvidenceCollectorAdapter(proof)
    raw = collect_evidence(command, adapter=adapter)
    bundle = extract_evidence(raw)

    # evidence-collected stage: the deployment digest/lane are visible in the
    # raw payload provenance and proof reference is in the raw payload refs.
    assert raw.source_commit_sha == proof.source_sha
    assert f"deployment:{proof.deployment_id}" in raw.raw_payload_refs
    assert raw.provenance["image_digest"] == proof.image_digest
    assert raw.provenance["runtime_lane"] == proof.runtime_lane.value

    # evidence-extracted stage: bundle is deterministic and threads the proof
    # facts through provenance for the downstream validator (Phase 5).
    assert bundle.evidence_bundle_hash.startswith("sha256:")
    assert bundle.provenance["image_digest"] == proof.image_digest
    assert bundle.provenance["promotion_batch_id"] == proof.promotion_batch_id
    assert f"deployment:{proof.deployment_id}" in bundle.provenance["raw_payload_refs"]

    # Determinism: same proof -> same bundle hash.
    raw_again = collect_evidence(
        command, adapter=DeploymentProofEvidenceCollectorAdapter(proof)
    )
    bundle_again = extract_evidence(raw_again)
    assert bundle_again.evidence_bundle_hash == bundle.evidence_bundle_hash


def _validation_result() -> ModelEvidenceValidationResult:
    return ModelEvidenceValidationResult(
        correlation_id=str(_CORRELATION_ID),
        validation_run_id="run-omn-12579",
        ticket_id="OMN-12579",
        repository="omnimarket",
        contract_hash="sha256:contract",
        evidence_bundle_hash="sha256:bundle",
        verifier_identity="omnimarket.node_occ_evidence_validator_compute",
        validator_version="evidence-readiness-native-v1",
        validated_at="2026-06-01T12:00:01Z",
        validation_state="PASSED",
        evidence_lifecycle_state="VALIDATED",
        topology_affecting=True,
        evidence_refs=(f"deployment:{_DEPLOYMENT_ID}",),
    )


def test_occ_pr_writer_consumes_evidence_validated_event() -> None:
    # The OCC PR writer's wiring contract: it consumes a ModelEvidenceValidationResult
    # (the payload of onex.evt.omnimarket.evidence-validated.v1) and emits a
    # provisional OCC PR reference (onex.evt.omnimarket.occ-pr-created.v1).
    validation = _validation_result()

    occ_pr = HandlerOccPrWriterEffect().handle(validation)

    assert occ_pr.ticket_id == "OMN-12579"
    assert occ_pr.correlation_id == str(_CORRELATION_ID)
    assert occ_pr.validation_run_id == "run-omn-12579"
    assert occ_pr.evidence_lifecycle_state == "PROVISIONAL"
    assert occ_pr.occ_repository == "OmniNode-ai/onex_change_control"
    assert occ_pr.pr_url.endswith(f"/pull/{occ_pr.pr_number}")


def test_occ_pr_writer_consumer_subscribes_to_evidence_validated_topic() -> None:
    contract_path = "src/omnimarket/nodes/node_occ_pr_writer_effect/contract.yaml"
    with open(contract_path, encoding="utf-8") as handle:
        contract = yaml.safe_load(handle)

    subscribe = set(contract["event_bus"]["subscribe_topics"])
    publish = set(contract["event_bus"]["publish_topics"])
    assert "onex.evt.omnimarket.evidence-validated.v1" in subscribe
    assert "onex.evt.omnimarket.occ-pr-created.v1" in publish
    # The wired consumer entrypoint must accept the validated-evidence model.
    assert contract["input_model"]["name"] == "ModelEvidenceValidationResult"


def test_collector_handler_accepts_proof_derived_command() -> None:
    # The collector effect handler must accept the deployment-proof-derived
    # command unchanged (it is a ModelEvidencePipelineCommand), proving the
    # bridge does not require a bespoke collector entrypoint.
    proof = _proof()
    command = evidence_command_from_deployment_proof(proof, ticket_id="OMN-12579")

    raw = HandlerEvidenceCollectorEffect(
        adapter=DeploymentProofEvidenceCollectorAdapter(proof)
    ).handle(command)
    bundle = HandlerEvidenceExtractorCompute().handle(raw)

    assert raw.ticket_id == "OMN-12579"
    assert bundle.source_commit_sha == proof.source_sha

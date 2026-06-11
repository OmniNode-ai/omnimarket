"""Native deterministic evidence/readiness pipeline domain helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from omnibase_compat.contracts.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_bundle import (
    ModelEvidenceBundle,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_pipeline_command import (
    ModelEvidencePipelineCommand,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_gap_report import (
    ModelGapReport,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_occ_pr_reference import (
    ModelOccPrReference,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_raw_evidence_payload import (
    ModelRawEvidencePayload,
)
from omnibase_compat.contracts.evidence_pipeline.wire.types import (
    EvidenceLifecycleState,
    GapClassification,
    ReadinessState,
    TriggerSurface,
    ValidationState,
)
from pydantic import BaseModel

from omnimarket.events.runtime_deployment import (
    ModelRuntimeDeploymentProof,
)

VALIDATOR_VERSION = "evidence-readiness-native-v1"
COLLECTOR_IDENTITY = "omnimarket.node_evidence_collector_effect"
DEPLOYMENT_PROOF_COLLECTOR_IDENTITY = (
    "omnimarket.node_evidence_collector_effect.deployment_proof"
)
VERIFIER_IDENTITY = "omnimarket.node_contract_matcher_compute"
WRITER_IDENTITY = "omnimarket.node_occ_pr_writer_effect"
DEFAULT_OCC_REPOSITORY = "OmniNode-ai/onex_change_control"
DEFAULT_DEPLOYMENT_PROOF_REPOSITORY = "omnimarket"
DEPLOY_TRIGGER_SURFACE: TriggerSurface = "deploy"

type TypedEvidenceEvent = (
    ModelEvidenceValidationResult | ModelDeploymentReadinessResult | ModelOccPrReference
)


class EvidenceCollectorAdapter(Protocol):
    """Effect boundary for gathering external evidence surfaces."""

    def collect(
        self, command: ModelEvidencePipelineCommand
    ) -> Mapping[str, object] | ModelRawEvidencePayload:
        """Return raw evidence fields for the command."""


class OccPrWriterAdapter(Protocol):
    """Effect boundary for writing OCC evidence PRs."""

    def write_pr(
        self, validation: ModelEvidenceValidationResult
    ) -> Mapping[str, object] | ModelOccPrReference:
        """Create or reuse an OCC PR reference."""


class LinearEvidenceUpdaterAdapter(Protocol):
    """Effect boundary for advisory Linear updates."""

    def update_evidence(
        self, validation: ModelEvidenceValidationResult
    ) -> Mapping[str, str]:
        """Annotate Linear and return deterministic acknowledgement metadata."""


class EvidencePublisherAdapter(Protocol):
    """Effect boundary for publishing typed evidence events."""

    def publish(self, event: TypedEvidenceEvent) -> Mapping[str, str]:
        """Publish an event and return acknowledgement metadata."""


class EvidencePipelinePorts(Protocol):
    """Typed node boundary used by the pipeline orchestrator."""

    def collect(self, command: ModelEvidencePipelineCommand) -> ModelRawEvidencePayload:
        """Run evidence collection."""

    def extract(self, raw: ModelRawEvidencePayload) -> ModelEvidenceBundle:
        """Run evidence extraction."""

    def match_contract(
        self, bundle: ModelEvidenceBundle
    ) -> ModelEvidenceValidationResult:
        """Run deterministic contract matching."""

    def write_occ_pr(
        self, validation: ModelEvidenceValidationResult
    ) -> ModelOccPrReference:
        """Run OCC PR writing."""

    def update_linear(
        self, validation: ModelEvidenceValidationResult
    ) -> ModelEvidenceValidationResult:
        """Run advisory Linear update."""

    def publish(self, event: TypedEvidenceEvent) -> TypedEvidenceEvent:
        """Run typed event publishing."""


class ReadinessGatePorts(Protocol):
    """Typed node boundary used by the readiness gate orchestrator."""

    def score(self, gap_report: ModelGapReport) -> ModelDeploymentReadinessResult:
        """Run readiness scoring."""

    def publish_gate(
        self, readiness: ModelDeploymentReadinessResult
    ) -> ModelDeploymentReadinessResult:
        """Publish the authoritative gate decision."""


class DeterministicEvidenceCollectorAdapter:
    """Local deterministic collector that uses command metadata as fixtures."""

    def collect(self, command: ModelEvidencePipelineCommand) -> Mapping[str, object]:
        metadata = dict(command.metadata)
        deployment_id = command.deployment_id or metadata.get("deployment_id")
        raw_refs = _split_csv(metadata.get("raw_payload_refs"))
        if deployment_id:
            raw_refs = (*raw_refs, f"deployment:{deployment_id}")
        return {
            "collected_at": metadata.get("collected_at", command.requested_at),
            "collector_identity": metadata.get(
                "collector_identity", COLLECTOR_IDENTITY
            ),
            "source_surfaces": _split_csv(
                metadata.get("source_surfaces"), default=("git", "github", "ci")
            ),
            "source_ci_run": metadata.get("source_ci_run"),
            "changed_files": _split_csv(metadata.get("changed_files")),
            "diff_refs": _split_csv(metadata.get("diff_refs")),
            "ci_artifact_refs": _split_csv(metadata.get("ci_artifact_refs")),
            "test_output_refs": _split_csv(metadata.get("test_output_refs")),
            "raw_payload_refs": raw_refs,
            "provenance": metadata,
        }


def _deployment_proof_metadata(
    proof: ModelRuntimeDeploymentProof,
) -> dict[str, str]:
    """Flatten deployment-proof facts into deterministic string metadata.

    These keys are consumed by ``DeterministicEvidenceCollectorAdapter`` and
    threaded through ``extract_evidence`` so the downstream validator (Phase 5)
    sees the digest/lane/promotion facts that the deployment actually proved.
    """
    metadata: dict[str, str] = {
        "deployment_id": str(proof.deployment_id),
        "image_digest": proof.image_digest,
        "runtime_lane": proof.runtime_lane.value,
        "compose_project": proof.compose_project,
        "health_status": proof.health_status,
        "ready_status": proof.ready_status,
        "deployment_status": proof.status,
        "collected_at": proof.probed_at.isoformat(),
        "collector_identity": DEPLOYMENT_PROOF_COLLECTOR_IDENTITY,
        "source_surfaces": "deploy,runtime,projection",
        "raw_payload_refs": f"deployment:{proof.deployment_id}",
    }
    if proof.promotion_batch_id is not None:
        metadata["promotion_batch_id"] = proof.promotion_batch_id
    if proof.topology_manifest_sha256 is not None:
        metadata["topology_manifest_sha256"] = proof.topology_manifest_sha256
    if proof.runtime_source_hash is not None:
        metadata["runtime_source_hash"] = proof.runtime_source_hash
    if proof.runtime_sweep_input_ref is not None:
        metadata["runtime_sweep_input_ref"] = proof.runtime_sweep_input_ref
    if proof.runtime_addresses:
        metadata["runtime_addresses"] = ",".join(proof.runtime_addresses)
    if proof.consumer_groups:
        metadata["consumer_groups"] = ",".join(proof.consumer_groups)
    for package, version in sorted(proof.package_versions.items()):
        metadata[f"package_version:{package}"] = version
    return metadata


def evidence_command_from_deployment_proof(
    proof: ModelRuntimeDeploymentProof,
    *,
    ticket_id: str,
    repository: str = DEFAULT_DEPLOYMENT_PROOF_REPOSITORY,
    validation_run_id: str | None = None,
) -> ModelEvidencePipelineCommand:
    """Build an evidence pipeline command from a runtime deployment proof.

    This is the Phase 4 bridge that drives ``evidence-collected ->
    evidence-extracted`` collection *from deployment proof*: deployment facts
    become the trigger for the evidence pipeline rather than an ad-hoc PR event.
    The command is deterministic for a given proof so the collected/extracted
    bundle hashes are stable.
    """
    metadata = _deployment_proof_metadata(proof)
    return ModelEvidencePipelineCommand(
        correlation_id=str(proof.correlation_id),
        validation_run_id=validation_run_id or f"deploy:{proof.deployment_id}",
        ticket_id=ticket_id,
        repository=repository,
        source_commit_sha=proof.source_sha,
        requested_at=proof.probed_at.isoformat(),
        trigger_surface=DEPLOY_TRIGGER_SURFACE,
        deployment_id=str(proof.deployment_id),
        topology_affecting=proof.topology_manifest_sha256 is not None,
        metadata=metadata,
    )


class DeploymentProofEvidenceCollectorAdapter:
    """Collector adapter that derives raw evidence from a deployment proof.

    The deployment proof is the authoritative evidence surface for a
    deploy-triggered pipeline run, so this adapter materializes the raw
    evidence payload directly from the proof instead of re-reading GitHub/CI.
    """

    def __init__(self, proof: ModelRuntimeDeploymentProof) -> None:
        self._proof = proof

    def collect(self, command: ModelEvidencePipelineCommand) -> Mapping[str, object]:
        metadata = {**_deployment_proof_metadata(self._proof), **dict(command.metadata)}
        return {
            "collected_at": metadata.get("collected_at", command.requested_at),
            "collector_identity": DEPLOYMENT_PROOF_COLLECTOR_IDENTITY,
            "source_surfaces": _split_csv(
                metadata.get("source_surfaces"),
                default=("deploy", "runtime", "projection"),
            ),
            "source_ci_run": metadata.get("runtime_sweep_input_ref"),
            "changed_files": _split_csv(metadata.get("changed_files")),
            "diff_refs": _split_csv(metadata.get("diff_refs")),
            "ci_artifact_refs": _split_csv(metadata.get("ci_artifact_refs")),
            "test_output_refs": _split_csv(metadata.get("test_output_refs")),
            "raw_payload_refs": (f"deployment:{self._proof.deployment_id}",),
            "provenance": metadata,
        }


class DeterministicOccPrWriterAdapter:
    """Local deterministic OCC writer used when no live adapter is injected."""

    def write_pr(
        self, validation: ModelEvidenceValidationResult
    ) -> Mapping[str, object]:
        key = _validation_result_hash(validation)
        pr_number = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:6], 16)
        pr_number = pr_number % 900000 + 1
        ticket_slug = validation.ticket_id.lower().replace("_", "-")
        branch = f"occ/{ticket_slug}/{validation.validation_run_id}"
        return {
            "occ_repository": DEFAULT_OCC_REPOSITORY,
            "pr_number": pr_number,
            "pr_url": f"https://github.com/{DEFAULT_OCC_REPOSITORY}/pull/{pr_number}",
            "branch": branch,
            "created_at": validation.validated_at,
            "writer_identity": WRITER_IDENTITY,
            "validation_result_hash": key,
            "idempotency_key": f"{validation.ticket_id}:{validation.validation_run_id}",
        }


class NoopLinearEvidenceUpdaterAdapter:
    """Deterministic Linear adapter for dry-run/local advisory annotation."""

    def update_evidence(
        self, validation: ModelEvidenceValidationResult
    ) -> Mapping[str, str]:
        return {
            "linear_update": "acknowledged",
            "linear_ticket": validation.ticket_id,
            "validation_run_id": validation.validation_run_id,
        }


class NoopEvidencePublisherAdapter:
    """Deterministic publisher adapter for local typed event publication."""

    def publish(self, event: TypedEvidenceEvent) -> Mapping[str, str]:
        return {
            "published": "true",
            "event_hash": _model_hash(event),
        }


class NativeEvidencePipelinePorts:
    """Default typed local ports without direct cross-node handler imports."""

    def __init__(
        self,
        *,
        collector: EvidenceCollectorAdapter | None = None,
        occ_writer: OccPrWriterAdapter | None = None,
        linear_updater: LinearEvidenceUpdaterAdapter | None = None,
        publisher: EvidencePublisherAdapter | None = None,
    ) -> None:
        self._collector = collector or DeterministicEvidenceCollectorAdapter()
        self._occ_writer = occ_writer or DeterministicOccPrWriterAdapter()
        self._linear_updater = linear_updater or NoopLinearEvidenceUpdaterAdapter()
        self._publisher = publisher or NoopEvidencePublisherAdapter()

    def collect(self, command: ModelEvidencePipelineCommand) -> ModelRawEvidencePayload:
        return collect_evidence(command, adapter=self._collector)

    def extract(self, raw: ModelRawEvidencePayload) -> ModelEvidenceBundle:
        return extract_evidence(raw)

    def match_contract(
        self, bundle: ModelEvidenceBundle
    ) -> ModelEvidenceValidationResult:
        return match_contract(bundle)

    def write_occ_pr(
        self, validation: ModelEvidenceValidationResult
    ) -> ModelOccPrReference:
        return write_occ_pr(validation, adapter=self._occ_writer)

    def update_linear(
        self, validation: ModelEvidenceValidationResult
    ) -> ModelEvidenceValidationResult:
        return update_linear(validation, adapter=self._linear_updater)

    def publish(self, event: TypedEvidenceEvent) -> TypedEvidenceEvent:
        return publish_evidence(event, adapter=self._publisher)


class NativeReadinessGatePorts:
    """Default typed readiness gate ports."""

    def __init__(self, *, publisher: EvidencePublisherAdapter | None = None) -> None:
        self._publisher = publisher or NoopEvidencePublisherAdapter()

    def score(self, gap_report: ModelGapReport) -> ModelDeploymentReadinessResult:
        return score_readiness(gap_report)

    def publish_gate(
        self, readiness: ModelDeploymentReadinessResult
    ) -> ModelDeploymentReadinessResult:
        return cast(
            ModelDeploymentReadinessResult,
            publish_evidence(readiness, adapter=self._publisher),
        )


def collect_evidence(
    command: ModelEvidencePipelineCommand,
    *,
    adapter: EvidenceCollectorAdapter | None = None,
) -> ModelRawEvidencePayload:
    collector = adapter or DeterministicEvidenceCollectorAdapter()
    collected = collector.collect(command)
    if isinstance(collected, ModelRawEvidencePayload):
        return collected
    payload = dict(collected)
    return ModelRawEvidencePayload(
        correlation_id=command.correlation_id,
        validation_run_id=command.validation_run_id,
        ticket_id=command.ticket_id,
        repository=command.repository,
        source_commit_sha=command.source_commit_sha,
        collected_at=str(payload.get("collected_at") or command.requested_at),
        collector_identity=str(payload.get("collector_identity") or COLLECTOR_IDENTITY),
        source_surfaces=tuple(
            _as_str_sequence(payload.get("source_surfaces")) or ("git",)
        ),
        source_pr=command.source_pr,
        source_ci_run=_optional_str(payload.get("source_ci_run")),
        changed_files=tuple(_as_str_sequence(payload.get("changed_files"))),
        diff_refs=tuple(_as_str_sequence(payload.get("diff_refs"))),
        ci_artifact_refs=tuple(_as_str_sequence(payload.get("ci_artifact_refs"))),
        test_output_refs=tuple(_as_str_sequence(payload.get("test_output_refs"))),
        raw_payload_refs=tuple(_as_str_sequence(payload.get("raw_payload_refs"))),
        provenance=_string_mapping(payload.get("provenance")),
    )


def extract_evidence(raw: ModelRawEvidencePayload) -> ModelEvidenceBundle:
    provenance = dict(raw.provenance)
    provenance.update(
        {
            "collector_identity": raw.collector_identity,
            "diff_refs": ",".join(raw.diff_refs),
            "ci_artifact_refs": ",".join(raw.ci_artifact_refs),
            "test_output_refs": ",".join(raw.test_output_refs),
            "raw_payload_refs": ",".join(raw.raw_payload_refs),
        }
    )
    source_projection_refs = tuple(
        ref for ref in raw.raw_payload_refs if ref.startswith("projection:")
    )
    test_results = {
        key.removeprefix("test:"): value
        for key, value in provenance.items()
        if key.startswith("test:")
    }
    if raw.test_output_refs and not test_results:
        test_results = dict.fromkeys(raw.test_output_refs, "passed")
    scope = _scope_from_changed_files(raw.changed_files)
    bundle_seed = {
        "correlation_id": raw.correlation_id,
        "validation_run_id": raw.validation_run_id,
        "ticket_id": raw.ticket_id,
        "repository": raw.repository,
        "source_commit_sha": raw.source_commit_sha,
        "source_surfaces": raw.source_surfaces,
        "changed_files": raw.changed_files,
        "diff_refs": raw.diff_refs,
        "ci_artifact_refs": raw.ci_artifact_refs,
        "test_output_refs": raw.test_output_refs,
        "raw_payload_refs": raw.raw_payload_refs,
        "provenance": provenance,
    }
    return ModelEvidenceBundle(
        correlation_id=raw.correlation_id,
        validation_run_id=raw.validation_run_id,
        ticket_id=raw.ticket_id,
        repository=raw.repository,
        source_surfaces=raw.source_surfaces,
        source_commit_sha=raw.source_commit_sha,
        evidence_bundle_hash=_hash_payload(bundle_seed),
        validator_version=VALIDATOR_VERSION,
        extracted_at=raw.collected_at,
        source_pr=raw.source_pr,
        source_ci_run=raw.source_ci_run,
        source_projection_refs=source_projection_refs,
        changed_files=raw.changed_files,
        test_results=test_results,
        scope=scope,
        provenance=provenance,
    )


def match_contract(bundle: ModelEvidenceBundle) -> ModelEvidenceValidationResult:
    provenance = dict(bundle.provenance)
    contract_hash = provenance.get("contract_hash") or _hash_payload(
        {
            "ticket_id": bundle.ticket_id,
            "repository": bundle.repository,
            "validator_version": VALIDATOR_VERSION,
        }
    )
    required_dod = set(_split_csv(provenance.get("dod_items")))
    satisfied_dod = set(_split_csv(provenance.get("satisfied_dod_items")))
    if not required_dod and bundle.test_results:
        required_dod = set(bundle.test_results)
        satisfied_dod = {
            key
            for key, value in bundle.test_results.items()
            if value.casefold() in {"passed", "pass", "ok", "true"}
        }
    missing_dod = tuple(sorted(required_dod - satisfied_dod))
    expected_scope = set(_split_csv(provenance.get("expected_scope")))
    actual_scope = set(bundle.scope)
    scope_drift_detected = bool(
        expected_scope and not actual_scope.issubset(expected_scope)
    )
    blocking_reason_codes: list[str] = []
    if missing_dod:
        blocking_reason_codes.append("missing_dod_items")
    if scope_drift_detected:
        blocking_reason_codes.append("scope_drift_detected")
    if provenance.get("contract_hash_mismatch", "").casefold() == "true":
        blocking_reason_codes.append("contract_hash_mismatch")
    validation_state: ValidationState = "FAILED" if blocking_reason_codes else "PASSED"
    lifecycle: EvidenceLifecycleState = (
        "VALIDATED" if validation_state == "PASSED" else "REJECTED"
    )
    requirement_results = {
        item: "passed" if item in satisfied_dod else "missing"
        for item in sorted(required_dod)
    }
    evidence_refs = _validation_evidence_refs(bundle)
    return ModelEvidenceValidationResult(
        correlation_id=bundle.correlation_id,
        validation_run_id=bundle.validation_run_id,
        ticket_id=bundle.ticket_id,
        repository=bundle.repository,
        contract_hash=str(contract_hash),
        evidence_bundle_hash=bundle.evidence_bundle_hash,
        verifier_identity=VERIFIER_IDENTITY,
        validator_version=VALIDATOR_VERSION,
        validated_at=bundle.extracted_at,
        validation_state=validation_state,
        evidence_lifecycle_state=lifecycle,
        topology_affecting=_bool_value(provenance.get("topology_affecting")),
        requirement_results=requirement_results,
        missing_dod_items=missing_dod,
        scope_drift_detected=scope_drift_detected,
        blocking_reason_codes=tuple(blocking_reason_codes),
        evidence_refs=evidence_refs,
    )


def analyze_gaps(validation: ModelEvidenceValidationResult) -> ModelGapReport:
    refs = (_validation_result_hash(validation),)
    classifications: dict[str, GapClassification] = {}
    missing_refs: list[str] = []
    hash_mismatch_refs: list[str] = []
    receipt_missing_refs: list[str] = []
    failed_refs: list[str] = []
    unknown_refs: list[str] = []
    for item in validation.missing_dod_items:
        key = f"dod:{item}"
        classifications[key] = "MISSING"
        missing_refs.append(key)
    if validation.validation_state == "FAILED":
        classifications["validation_state"] = "VALIDATION_FAILED"
        failed_refs.append(refs[0])
    for reason in validation.blocking_reason_codes:
        if "hash" in reason:
            classifications[reason] = "HASH_MISMATCH"
            hash_mismatch_refs.append(reason)
        elif "receipt" in reason:
            classifications[reason] = "RECEIPT_MISSING"
            receipt_missing_refs.append(reason)
        elif reason not in {"missing_dod_items"}:
            classifications[reason] = "UNKNOWN"
            unknown_refs.append(reason)
    return ModelGapReport(
        correlation_id=validation.correlation_id,
        validation_run_id=validation.validation_run_id,
        deployment_id=_deployment_id_from_refs(
            validation.evidence_refs,
            fallback=f"ticket:{validation.ticket_id}",
        ),
        generated_at=validation.validated_at,
        validator_version=VALIDATOR_VERSION,
        gap_classifications=classifications,
        validation_result_refs=refs,
        missing_evidence_refs=tuple(missing_refs),
        hash_mismatch_refs=tuple(hash_mismatch_refs),
        receipt_missing_refs=tuple(receipt_missing_refs),
        failed_validation_refs=tuple(failed_refs),
        unknown_refs=tuple(unknown_refs),
    )


def score_readiness(gap_report: ModelGapReport) -> ModelDeploymentReadinessResult:
    blocking_classifications = {
        "MISSING",
        "HASH_MISMATCH",
        "RECEIPT_MISSING",
        "VALIDATION_FAILED",
        "UNKNOWN",
    }
    degraded_classifications = {"STALE", "SUPERSEDED"}
    classifications = set(gap_report.gap_classifications.values())
    if classifications & blocking_classifications:
        readiness_state: ReadinessState = "BLOCKED"
    elif classifications & degraded_classifications:
        readiness_state = "DEGRADED"
    else:
        readiness_state = "READY"
    blocking_reasons = tuple(
        sorted(
            key
            for key, value in gap_report.gap_classifications.items()
            if value in blocking_classifications
        )
    )
    return ModelDeploymentReadinessResult(
        correlation_id=gap_report.correlation_id,
        validation_run_id=gap_report.validation_run_id,
        deployment_id=gap_report.deployment_id,
        readiness_state=readiness_state,
        scored_at=gap_report.generated_at,
        validator_version=VALIDATOR_VERSION,
        gap_report_hash=_model_hash(gap_report),
        topology_affecting=False,
        blocking_reason_codes=blocking_reasons,
        required_evidence_refs=gap_report.validation_result_refs,
        missing_contracts=gap_report.missing_evidence_refs,
        superseded_artifacts=gap_report.superseded_artifacts,
        stale_validation_windows=gap_report.stale_evidence_refs,
        unresolved_runtime_gaps=(
            *gap_report.receipt_missing_refs,
            *gap_report.failed_validation_refs,
            *gap_report.unknown_refs,
        ),
        topology_metadata={"validator_version": VALIDATOR_VERSION},
        receipt_refs=tuple(
            ref
            for ref in gap_report.validation_result_refs
            if ref.startswith("receipt:")
        ),
    )


def write_occ_pr(
    validation: ModelEvidenceValidationResult,
    *,
    adapter: OccPrWriterAdapter | None = None,
) -> ModelOccPrReference:
    writer = adapter or DeterministicOccPrWriterAdapter()
    written = writer.write_pr(validation)
    if isinstance(written, ModelOccPrReference):
        return written
    payload = dict(written)
    pr_number_value = payload.get("pr_number")
    pr_number = pr_number_value if isinstance(pr_number_value, int) else 1
    return ModelOccPrReference(
        correlation_id=validation.correlation_id,
        validation_run_id=validation.validation_run_id,
        ticket_id=validation.ticket_id,
        occ_repository=str(payload.get("occ_repository") or DEFAULT_OCC_REPOSITORY),
        pr_number=pr_number,
        pr_url=str(payload.get("pr_url") or ""),
        branch=str(payload.get("branch") or f"occ/{validation.validation_run_id}"),
        created_at=str(payload.get("created_at") or validation.validated_at),
        writer_identity=str(payload.get("writer_identity") or WRITER_IDENTITY),
        evidence_lifecycle_state=cast(
            EvidenceLifecycleState,
            payload.get("evidence_lifecycle_state") or "PROVISIONAL",
        ),
        validation_result_hash=_optional_str(payload.get("validation_result_hash"))
        or _validation_result_hash(validation),
        commit_sha=_optional_str(payload.get("commit_sha")),
        idempotency_key=_optional_str(payload.get("idempotency_key"))
        or f"{validation.ticket_id}:{validation.validation_run_id}",
    )


def update_linear(
    validation: ModelEvidenceValidationResult,
    *,
    adapter: LinearEvidenceUpdaterAdapter | None = None,
) -> ModelEvidenceValidationResult:
    updater = adapter or NoopLinearEvidenceUpdaterAdapter()
    ack = dict(updater.update_evidence(validation))
    refs = (
        *validation.evidence_refs,
        *tuple(f"linear:{key}={value}" for key, value in sorted(ack.items())),
    )
    return validation.model_copy(update={"evidence_refs": refs})


def publish_evidence(
    event: TypedEvidenceEvent,
    *,
    adapter: EvidencePublisherAdapter | None = None,
) -> TypedEvidenceEvent:
    publisher = adapter or NoopEvidencePublisherAdapter()
    publisher.publish(event)
    return event


def reduce_deployment_evidence(
    event: TypedEvidenceEvent,
) -> ModelDeploymentReadinessResult:
    """Derive deployment readiness from one append-only evidence event.

    Persistence into reducer-owned projection tables is owned by
    ``node_deployment_evidence_reducer``'s handler via the canonical projection
    database adapter; this helper only derives the readiness decision.
    """
    if isinstance(event, ModelDeploymentReadinessResult):
        return event
    if isinstance(event, ModelEvidenceValidationResult):
        return _readiness_from_validation(event)
    return _readiness_from_occ_pr(event)


def _readiness_from_validation(
    validation: ModelEvidenceValidationResult,
) -> ModelDeploymentReadinessResult:
    state: ReadinessState = (
        "READY" if validation.validation_state == "PASSED" else "BLOCKED"
    )
    return ModelDeploymentReadinessResult(
        correlation_id=validation.correlation_id,
        validation_run_id=validation.validation_run_id,
        deployment_id=_deployment_id_from_refs(
            validation.evidence_refs,
            fallback=f"ticket:{validation.ticket_id}",
        ),
        readiness_state=state,
        scored_at=validation.validated_at,
        validator_version=VALIDATOR_VERSION,
        gap_report_hash=_validation_result_hash(validation),
        topology_affecting=validation.topology_affecting,
        blocking_reason_codes=validation.blocking_reason_codes,
        required_evidence_refs=validation.evidence_refs,
        missing_contracts=validation.missing_dod_items,
    )


def _readiness_from_occ_pr(occ: ModelOccPrReference) -> ModelDeploymentReadinessResult:
    return ModelDeploymentReadinessResult(
        correlation_id=occ.correlation_id,
        validation_run_id=occ.validation_run_id,
        deployment_id=f"ticket:{occ.ticket_id}",
        readiness_state="ADVISORY_ONLY",
        scored_at=occ.created_at,
        validator_version=VALIDATOR_VERSION,
        gap_report_hash=occ.validation_result_hash or _model_hash(occ),
        required_evidence_refs=(occ.pr_url,),
        receipt_refs=(occ.pr_url,),
    )


# Transport-envelope keys that the runtime adds around the domain payload.
# When the dispatch engine materializes a ``ModelEventEnvelope`` to a dict it
# wraps the domain fields under ``payload`` and carries routing metadata
# alongside it. The domain models below never declare these keys, so they must
# be stripped before model construction.
#
# Two distinct outer shapes reach this boundary and BOTH must be recognised:
#
#   1. The dispatch-engine *materialized* shape — the only shape the live
#      runtime actually delivers to ``handle()`` for the evidence/readiness
#      contracts (their ``handler_routing`` declares no ``event_model``, so
#      auto-wiring passes the materialized dict raw). Outer keys are exactly
#      ``{"payload", "__bindings", "__debug_trace"}`` — the real
#      ``partition_key`` lives *inside* ``__debug_trace``, not at the top level.
#      See ``omnibase_infra.runtime.message_dispatch_engine.
#      _materialize_envelope_with_bindings``.
#   2. The bare envelope-field shape (``partition_key``/``event_type``/
#      ``correlation_id`` at the top level) — non-materialized deliveries.
#
# CONSISTENCY: this set is the omnimarket twin of
# ``omnibase_infra.runtime.auto_wiring.handler_wiring._ENVELOPE_MARKER_KEYS``
# (OMN-12940). A cross-repo shared constant would force a cross-repo dependency,
# so the two are kept in sync by hand and pinned by
# ``test_marker_set_covers_materialized_dispatch_keys`` (OMN-12946). Dropping the
# materialization markers (``__debug_trace``/``__bindings``) silently turns
# ``_unwrap_envelope`` back into a no-op on live dispatch — the OMN-12946 defect.
_ENVELOPE_MARKER_KEYS: frozenset[str] = frozenset(
    {
        "partition_key",
        "event_type",
        "envelope_id",
        "event_id",
        "correlation_id",
        "__debug_trace",
        "__bindings",
    }
)


def _unwrap_envelope(
    payload: BaseModel | Mapping[str, object],
) -> BaseModel | Mapping[str, object]:
    """Return the domain payload, unwrapping any transport envelope around it.

    The runtime delivers either a ``ModelEventEnvelope``-shaped object exposing
    a ``.payload`` attribute, or a materialized dict of the form
    ``{"payload": {...}, "partition_key": ...}``. Domain models do not declare
    the transport keys, so splatting the raw envelope into ``Model(**payload)``
    raises a ``ValidationError`` with every required field reported missing.
    Unwrapping recursively at the coercion boundary keeps the domain models
    transport-agnostic and fixes the shared ``coerce(**payload)`` defect class
    for the whole evidence/readiness node family.
    """
    # ModelEventEnvelope (or any object exposing a populated ``payload``).
    nested = getattr(payload, "payload", None)
    if nested is not None and not isinstance(payload, Mapping):
        return _unwrap_envelope(nested)
    # Only unwrap when the outer mapping is a transport envelope (carries a
    # ``payload`` mapping plus a transport marker), never when ``payload`` is a
    # legitimate domain field on the model itself.
    if (
        isinstance(payload, Mapping)
        and isinstance(payload.get("payload"), Mapping)
        and _ENVELOPE_MARKER_KEYS & payload.keys()
    ):
        return _unwrap_envelope(cast(Mapping[str, object], payload["payload"]))
    return payload


def unwrap_envelope(
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    """Public mapping-only wrapper over ``_unwrap_envelope`` (OMN-12935).

    Effect nodes that read raw dict fields (rather than constructing a typed
    wire model) — e.g. the dashboard projection effect — must strip the same
    transport envelope before reading ``correlation_id``/``event_id``; otherwise
    they read those off the outer envelope, fabricate a ``projection_cursor``,
    and emit a malformed correlation id downstream. The dashboard delivery is
    always a dict, so this narrows the shared helper's return to ``Mapping``.
    """
    unwrapped = _unwrap_envelope(payload)
    if isinstance(unwrapped, Mapping):
        return unwrapped
    return payload


def coerce_command(
    payload: ModelEvidencePipelineCommand | Mapping[str, object],
) -> ModelEvidencePipelineCommand:
    if isinstance(payload, ModelEvidencePipelineCommand):
        return payload
    unwrapped = _unwrap_envelope(payload)
    if isinstance(unwrapped, ModelEvidencePipelineCommand):
        return unwrapped
    return ModelEvidencePipelineCommand(**cast(Mapping[str, object], unwrapped))


def coerce_raw(
    payload: ModelRawEvidencePayload | Mapping[str, object],
) -> ModelRawEvidencePayload:
    if isinstance(payload, ModelRawEvidencePayload):
        return payload
    unwrapped = _unwrap_envelope(payload)
    if isinstance(unwrapped, ModelRawEvidencePayload):
        return unwrapped
    return ModelRawEvidencePayload(**cast(Mapping[str, object], unwrapped))


def coerce_bundle(
    payload: ModelEvidenceBundle | Mapping[str, object],
) -> ModelEvidenceBundle:
    if isinstance(payload, ModelEvidenceBundle):
        return payload
    unwrapped = _unwrap_envelope(payload)
    if isinstance(unwrapped, ModelEvidenceBundle):
        return unwrapped
    return ModelEvidenceBundle(**cast(Mapping[str, object], unwrapped))


def coerce_validation(
    payload: ModelEvidenceValidationResult | Mapping[str, object],
) -> ModelEvidenceValidationResult:
    if isinstance(payload, ModelEvidenceValidationResult):
        return payload
    unwrapped = _unwrap_envelope(payload)
    if isinstance(unwrapped, ModelEvidenceValidationResult):
        return unwrapped
    return ModelEvidenceValidationResult(**cast(Mapping[str, object], unwrapped))


def coerce_gap(payload: BaseModel | Mapping[str, object]) -> ModelGapReport:
    if isinstance(payload, ModelGapReport):
        return payload
    unwrapped = _unwrap_envelope(payload)
    if isinstance(unwrapped, ModelGapReport):
        return unwrapped
    return ModelGapReport(**cast(Mapping[str, object], unwrapped))


def coerce_readiness(
    payload: BaseModel | Mapping[str, object],
) -> ModelDeploymentReadinessResult:
    if isinstance(payload, ModelDeploymentReadinessResult):
        return payload
    unwrapped = _unwrap_envelope(payload)
    if isinstance(unwrapped, ModelDeploymentReadinessResult):
        return unwrapped
    return ModelDeploymentReadinessResult(**cast(Mapping[str, object], unwrapped))


def coerce_occ(
    payload: ModelOccPrReference | Mapping[str, object],
) -> ModelOccPrReference:
    if isinstance(payload, ModelOccPrReference):
        return payload
    unwrapped = _unwrap_envelope(payload)
    if isinstance(unwrapped, ModelOccPrReference):
        return unwrapped
    return ModelOccPrReference(**cast(Mapping[str, object], unwrapped))


def coerce_evidence_event(
    payload: TypedEvidenceEvent | Mapping[str, object],
) -> TypedEvidenceEvent:
    if isinstance(
        payload,
        (
            ModelEvidenceValidationResult,
            ModelDeploymentReadinessResult,
            ModelOccPrReference,
        ),
    ):
        return payload
    unwrapped = _unwrap_envelope(payload)
    if isinstance(
        unwrapped,
        (
            ModelEvidenceValidationResult,
            ModelDeploymentReadinessResult,
            ModelOccPrReference,
        ),
    ):
        return unwrapped
    fields = cast(Mapping[str, object], unwrapped)
    if "readiness_state" in fields:
        return ModelDeploymentReadinessResult(**fields)
    if "occ_repository" in fields:
        return ModelOccPrReference(**fields)
    return ModelEvidenceValidationResult(**fields)


def _validation_evidence_refs(bundle: ModelEvidenceBundle) -> tuple[str, ...]:
    refs: list[str] = []
    refs.extend(bundle.source_projection_refs)
    refs.extend(_split_csv(bundle.provenance.get("diff_refs")))
    refs.extend(_split_csv(bundle.provenance.get("ci_artifact_refs")))
    refs.extend(_split_csv(bundle.provenance.get("test_output_refs")))
    refs.extend(_split_csv(bundle.provenance.get("raw_payload_refs")))
    deployment_id = bundle.provenance.get("deployment_id")
    if deployment_id and f"deployment:{deployment_id}" not in refs:
        refs.append(f"deployment:{deployment_id}")
    return tuple(dict.fromkeys(refs))


def _deployment_id_from_refs(refs: Sequence[str], *, fallback: str) -> str:
    for ref in refs:
        if ref.startswith("deployment:"):
            return ref.removeprefix("deployment:")
    return fallback


def _validation_result_hash(validation: ModelEvidenceValidationResult) -> str:
    return _model_hash(validation)


def _model_hash(model: BaseModel) -> str:
    return _hash_payload(model.model_dump(mode="json"))


def _hash_payload(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _split_csv(value: object, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        items = tuple(item.strip() for item in value.split(",") if item.strip())
        return items or default
    return tuple(_as_str_sequence(value)) or default


def _as_str_sequence(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return tuple(str(item) for item in value if str(item))
    return (str(value),)


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items()}


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    rendered = str(value)
    return rendered if rendered else None


def _bool_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.casefold() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _scope_from_changed_files(changed_files: Sequence[str]) -> tuple[str, ...]:
    scopes = []
    for path in changed_files:
        normalized = path.strip("/")
        if not normalized:
            continue
        scopes.append(normalized.split("/", 1)[0])
    return tuple(sorted(set(scopes)))

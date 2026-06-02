"""Pure deterministic validator for model-generated OCC evidence drafts.

OMN-12580 (Phase 5). This compute node is pure: no I/O, no bus, no DB, no model
calls. It receives a provisional ``ModelOccEvidenceDraft`` plus the authoritative
deployment proof/pins and decides acceptance deterministically.

Blocker B4: the PASS path returns the EXISTING compat
``ModelEvidenceValidationResult`` so the existing
``node_occ_pr_writer_effect`` (which consumes
``onex.evt.omnimarket.evidence-validated.v1`` typed as that model) fires once
wired. The reject path returns the INTERNAL
``ModelOccEvidenceDraftValidationResult`` audit shape; the orchestrator publishes
that on ``onex.evt.omnimarket.occ-evidence-draft-rejected.v1``.

A local model may draft the OCC artifacts but may never mark them authoritative:
this validator owns acceptance.
"""

from __future__ import annotations

import hashlib

import yaml
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_validation_result import (
    ModelEvidenceValidationResult,
)

from omnimarket.events.occ_evidence import (
    DraftValidationState,
    EnumEvidenceLifecycleState,
    FreshnessStatus,
    ModelOccEvidenceDraft,
    ModelOccEvidenceDraftValidationResult,
    ValidationCheckStatus,
)
from omnimarket.nodes.node_occ_evidence_validator_compute.models.model_occ_evidence_validate_command import (
    ModelOccEvidenceValidateCommand,
)

VALIDATOR_VERSION = "occ-evidence-validator-compute-v1"
VERIFIER_IDENTITY = "omnimarket.node_occ_evidence_validator_compute"

# Result union: PASS yields the compat wire model the OCC PR writer consumes;
# FAIL yields the internal audit/reject model.
type OccEvidenceValidatorOutput = (
    ModelEvidenceValidationResult | ModelOccEvidenceDraftValidationResult
)


def _draft_hash(draft: ModelOccEvidenceDraft) -> str:
    """Recompute the content hash over the model-generated artifacts."""
    payload = "\n".join(
        (
            draft.ticket_id,
            draft.contract_yaml,
            draft.pr_body,
            *draft.receipt_yamls,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_contract(contract_yaml: str) -> dict[str, object] | None:
    """Parse the draft contract YAML, returning None on any structural failure."""
    try:
        parsed = yaml.safe_load(contract_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _parse_receipt_commands(receipt_yamls: tuple[str, ...]) -> list[str] | None:
    """Extract probe_command from each receipt YAML; None on any malformed doc.

    A receipt missing its ``probe_command`` (or with an empty command) yields
    None so the validator can reject the draft for a missing probe command.
    """
    commands: list[str] = []
    for raw in receipt_yamls:
        try:
            parsed = yaml.safe_load(raw)
        except yaml.YAMLError:
            return None
        if not isinstance(parsed, dict):
            return None
        command = parsed.get("probe_command")
        if not isinstance(command, str) or not command.strip():
            return None
        commands.append(command)
    return commands


def _check_freshness(
    command: ModelOccEvidenceValidateCommand,
) -> FreshnessStatus:
    """Topology proof freshness derived from the deployment proof."""
    proof = command.runtime_deployment_proof
    if proof.topology_manifest_sha256 is None:
        return "degraded"
    age = (command.validated_at - proof.probed_at).total_seconds()
    if age < 0:
        return "degraded"
    if age > command.topology_freshness_max_age_seconds:
        return "stale"
    return "current"


class HandlerOccEvidenceValidator:
    """Deterministically validate a provisional OCC draft against authoritative pins."""

    def handle(
        self, command: ModelOccEvidenceValidateCommand
    ) -> OccEvidenceValidatorOutput:
        draft = command.draft
        proof = command.runtime_deployment_proof

        blocking: list[str] = []

        # --- draft integrity: declared draft_hash must match recomputed content ---
        recomputed_hash = _draft_hash(draft)
        if recomputed_hash != draft.draft_hash:
            blocking.append("draft_hash_mismatch")

        # --- OCC YAML schema validity ---
        contract = _parse_contract(draft.contract_yaml)
        schema_status: ValidationCheckStatus
        if contract is None:
            schema_status = "fail"
            blocking.append("schema_invalid")
        else:
            schema_status = "pass"

        # --- ticket / repository / sha / digest / batch / lane match ---
        sha_match_status: ValidationCheckStatus = "skipped"
        image_digest_match_status: ValidationCheckStatus = "skipped"
        if contract is not None:
            if str(contract.get("ticket_id", "")) != draft.ticket_id:
                blocking.append("ticket_id_mismatch")
            if str(contract.get("repository", "")) != command.expected_repository:
                blocking.append("repository_mismatch")

            contract_sha = str(contract.get("source_sha", ""))
            if (
                contract_sha == command.expected_source_sha
                and proof.source_sha == command.expected_source_sha
            ):
                sha_match_status = "pass"
            else:
                sha_match_status = "fail"
                blocking.append("source_sha_mismatch")

            contract_digest = str(contract.get("image_digest", ""))
            if (
                contract_digest == command.expected_image_digest
                and proof.image_digest == command.expected_image_digest
            ):
                image_digest_match_status = "pass"
            else:
                image_digest_match_status = "fail"
                blocking.append("image_digest_mismatch")

            contract_lane = str(contract.get("runtime_lane", ""))
            if contract_lane != command.runtime_lane.value:
                blocking.append("runtime_lane_mismatch")

            contract_batch = contract.get("promotion_batch_id")
            expected_batch = command.expected_promotion_batch_id
            if (
                expected_batch is not None
                and str(contract_batch or "") != expected_batch
            ):
                blocking.append("promotion_batch_mismatch")
        else:
            sha_match_status = "fail"
            image_digest_match_status = "fail"

        # --- required receipt commands present and policy-allowed ---
        receipt_commands = _parse_receipt_commands(draft.receipt_yamls)
        receipt_probe_status: ValidationCheckStatus
        if receipt_commands is None:
            receipt_probe_status = "fail"
            blocking.append("receipt_probe_command_missing")
        else:
            present = set(receipt_commands)
            missing_required = [
                required
                for required in command.required_receipt_commands
                if required not in present
            ]
            allowed = set(command.allowed_receipt_commands)
            disallowed = [cmd for cmd in receipt_commands if cmd not in allowed]
            if missing_required:
                receipt_probe_status = "fail"
                blocking.append("receipt_command_missing")
            elif disallowed:
                receipt_probe_status = "fail"
                blocking.append("receipt_command_not_allowed")
            else:
                receipt_probe_status = "pass"

        # --- topology proof freshness ---
        topology_freshness_status = _check_freshness(command)
        if topology_freshness_status != "current":
            blocking.append("topology_proof_stale")

        # --- local Receipt Gate fixture ---
        if not command.receipt_gate_fixture_passes:
            blocking.append("receipt_gate_fixture_failed")

        # --- no model-only claim is marked authoritative ---
        # The draft model fixes evidence_lifecycle_state to PROVISIONAL; a draft
        # that asserts any other lifecycle state is a model self-attestation and
        # is rejected.
        if draft.evidence_lifecycle_state is not EnumEvidenceLifecycleState.PROVISIONAL:
            blocking.append("model_marked_authoritative")

        validation_state: DraftValidationState = "FAILED" if blocking else "PASSED"

        if validation_state == "FAILED":
            return ModelOccEvidenceDraftValidationResult(
                correlation_id=command.correlation_id,
                deployment_id=command.deployment_id,
                ticket_id=draft.ticket_id,
                draft_hash=draft.draft_hash,
                validation_state="FAILED",
                schema_status=schema_status,
                sha_match_status=sha_match_status,
                image_digest_match_status=image_digest_match_status,
                receipt_probe_status=receipt_probe_status,
                topology_freshness_status=topology_freshness_status,
                validated_at=command.validated_at,
                promotion_batch_id=command.expected_promotion_batch_id,
                blocking_reason_codes=tuple(blocking),
            )

        # PASS path: emit the EXISTING compat wire model the OCC PR writer consumes.
        return ModelEvidenceValidationResult(
            correlation_id=str(command.correlation_id),
            validation_run_id=str(command.deployment_id),
            ticket_id=draft.ticket_id,
            repository=command.expected_repository,
            contract_hash=_draft_hash(draft),
            evidence_bundle_hash=draft.draft_hash,
            verifier_identity=VERIFIER_IDENTITY,
            validator_version=VALIDATOR_VERSION,
            validated_at=command.validated_at.isoformat(),
            validation_state="PASSED",
            evidence_lifecycle_state="VALIDATED",
            topology_affecting=proof.topology_manifest_sha256 is not None,
            requirement_results=dict.fromkeys(receipt_commands or [], "passed"),
            evidence_refs=(
                f"deployment:{command.deployment_id}",
                f"digest:{command.expected_image_digest}",
                f"sha:{command.expected_source_sha}",
            ),
        )


__all__: list[str] = [
    "VALIDATOR_VERSION",
    "VERIFIER_IDENTITY",
    "HandlerOccEvidenceValidator",
    "OccEvidenceValidatorOutput",
]

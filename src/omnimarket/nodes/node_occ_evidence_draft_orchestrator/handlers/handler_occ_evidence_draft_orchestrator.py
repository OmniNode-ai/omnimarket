"""OCC evidence draft orchestrator handler (OMN-12580, Phase 5).

This orchestrator owns prompt construction and draft lifecycle for local-model
OCC drafting. It does NOT call any model directly: it builds a typed
``ModelDelegationRequest`` published on
``onex.cmd.omnibase-infra.delegation-request.v1`` (the live delegation surface)
and reacts to ``onex.evt.omnibase-infra.inference-response.v1`` by materializing a
PROVISIONAL ``ModelOccEvidenceDraft``. It never marks a draft authoritative;
acceptance is decided downstream by ``node_occ_evidence_validator_compute``.

Flow:
1. consume ``onex.cmd.omnimarket.occ-evidence-draft-start.v1`` (ModelOccEvidenceDraftRequest)
   -> build ModelDelegationRequest (publish on delegation-request.v1).
2. consume ``onex.evt.omnibase-infra.inference-response.v1`` (ModelInferenceResponseData)
   -> parse content -> publish ``onex.evt.omnimarket.occ-evidence-draft-created.v1``
      (ModelOccEvidenceDraft, PROVISIONAL) or
      ``onex.evt.omnimarket.occ-evidence-draft-failed.v1`` (ModelOccEvidenceDraftFailed).

routing-decision.v1 and quality-gate-result.v1 are consumed for delegation
lifecycle observability; they do not change the draft content.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_occ_evidence_draft_orchestrator.models.model_occ_draft_model_response import (
    ModelOccDraftModelResponse,
)
from omnimarket.nodes.node_occ_evidence_draft_orchestrator.models.model_occ_evidence_draft_failed import (
    EnumOccDraftFailureReason,
    ModelOccEvidenceDraftFailed,
)
from omnimarket.nodes.node_redeploy.models.model_occ_evidence_draft import (
    EnumEvidenceLifecycleState,
    ModelOccEvidenceDraft,
    ModelOccEvidenceDraftRequest,
)

ORCHESTRATOR_IDENTITY = "omnimarket.node_occ_evidence_draft_orchestrator"

type DraftOutcome = ModelOccEvidenceDraft | ModelOccEvidenceDraftFailed


def _draft_hash(
    ticket_id: str, contract_yaml: str, pr_body: str, receipt_yamls: tuple[str, ...]
) -> str:
    payload = "\n".join((ticket_id, contract_yaml, pr_body, *receipt_yamls))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_prompt(request: ModelOccEvidenceDraftRequest) -> str:
    """Deterministic prompt instructing the model to produce OCC draft JSON."""
    proof = request.runtime_deployment_proof
    proof_block = "no deployment proof provided"
    if proof is not None:
        proof_block = (
            f"runtime_lane={proof.runtime_lane.value} "
            f"source_sha={proof.source_sha} "
            f"image_digest={proof.image_digest} "
            f"compose_project={proof.compose_project}"
        )
    required = ", ".join(request.required_receipts) or "(none specified)"
    return (
        "Draft OmniNode Change Control (OCC) evidence for a runtime deployment. "
        "Return ONE JSON object with keys: contract_yaml (string), pr_body "
        "(string), receipt_yamls (array of strings, each a DoD receipt YAML with a "
        "probe_command). The draft is PROVISIONAL; do not claim it is authoritative.\n"
        f"ticket_id: {request.ticket_id}\n"
        f"runtime_lane: {request.runtime_lane.value}\n"
        f"target_occ_repo: {request.target_occ_repo}\n"
        f"required_receipt_commands: {required}\n"
        f"deployment_proof: {proof_block}\n"
    )


class HandlerOccEvidenceDraftOrchestrator:
    """Build delegation requests and materialize PROVISIONAL OCC drafts."""

    def build_delegation_request(
        self, request: ModelOccEvidenceDraftRequest
    ) -> ModelDelegationRequest:
        """Build the typed delegation command for local-model OCC drafting.

        Published on ``onex.cmd.omnibase-infra.delegation-request.v1``. The node
        never invokes a model directly — the runtime routes this command to the
        live delegation orchestrator.
        """
        return ModelDelegationRequest(
            prompt=_build_prompt(request),
            task_type="code_generation",
            correlation_id=request.correlation_id,
            emitted_at=request.requested_at,
        )

    def materialize_draft(
        self,
        request: ModelOccEvidenceDraftRequest,
        response: ModelInferenceResponseData,
    ) -> DraftOutcome:
        """React to the inference response by building a PROVISIONAL draft.

        Returns a ``ModelOccEvidenceDraft`` (published on
        ``occ-evidence-draft-created.v1``) on success, or a
        ``ModelOccEvidenceDraftFailed`` (published on
        ``occ-evidence-draft-failed.v1``) on delegation/parse failure.
        """
        now = datetime.now(UTC)
        model_identity = response.model_used

        if response.error_message:
            return ModelOccEvidenceDraftFailed(
                correlation_id=request.correlation_id,
                deployment_id=request.deployment_id,
                ticket_id=request.ticket_id,
                failure_reason=EnumOccDraftFailureReason.DELEGATION_ERROR,
                detail=response.error_message,
                model_identity=model_identity,
                failed_at=now,
            )

        content = response.content.strip()
        if not content:
            return ModelOccEvidenceDraftFailed(
                correlation_id=request.correlation_id,
                deployment_id=request.deployment_id,
                ticket_id=request.ticket_id,
                failure_reason=EnumOccDraftFailureReason.EMPTY_RESPONSE,
                detail="model returned empty content",
                model_identity=model_identity,
                failed_at=now,
            )

        try:
            raw = json.loads(content)
        except json.JSONDecodeError as exc:
            return ModelOccEvidenceDraftFailed(
                correlation_id=request.correlation_id,
                deployment_id=request.deployment_id,
                ticket_id=request.ticket_id,
                failure_reason=EnumOccDraftFailureReason.UNPARSEABLE_RESPONSE,
                detail=f"model content is not valid JSON: {exc}",
                model_identity=model_identity,
                failed_at=now,
            )

        try:
            parsed = ModelOccDraftModelResponse.model_validate(raw)
        except ValueError as exc:
            return ModelOccEvidenceDraftFailed(
                correlation_id=request.correlation_id,
                deployment_id=request.deployment_id,
                ticket_id=request.ticket_id,
                failure_reason=EnumOccDraftFailureReason.MISSING_ARTIFACTS,
                detail=f"model content missing required OCC artifacts: {exc}",
                model_identity=model_identity,
                failed_at=now,
            )

        draft_hash = _draft_hash(
            request.ticket_id,
            parsed.contract_yaml,
            parsed.pr_body,
            parsed.receipt_yamls,
        )
        return ModelOccEvidenceDraft(
            correlation_id=request.correlation_id,
            deployment_id=request.deployment_id,
            ticket_id=request.ticket_id,
            draft_hash=draft_hash,
            contract_yaml=parsed.contract_yaml,
            pr_body=parsed.pr_body,
            model_identity=model_identity,
            generated_at=now,
            receipt_yamls=parsed.receipt_yamls,
            evidence_lifecycle_state=EnumEvidenceLifecycleState.PROVISIONAL,
        )


__all__: list[str] = [
    "ORCHESTRATOR_IDENTITY",
    "DraftOutcome",
    "HandlerOccEvidenceDraftOrchestrator",
]

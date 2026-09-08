"""Pure validation of supplied V4 artifact evidence; no collector or runtime exists."""

from typing import Literal

from omnimarket.nodes.node_rsd_v4_artifact_evidence_validate_compute.models.model_v4_artifact_evidence import (
    ModelV4ArtifactEvidenceValidationInput,
    ModelV4ArtifactEvidenceValidationOutput,
)
from omnimarket.rsd.v4_artifact_evidence import (
    parse_container_bootstrap_artifact_evidence_closure_v4_canonical_json,
    parse_container_bootstrap_build_worker_trust_policy_v4_canonical_json,
    validate_container_bootstrap_artifact_evidence_closure_v4,
)
from omnimarket.rsd.v4_static_profile import (
    parse_container_bootstrap_static_profile_trust_anchor_v4_canonical_json,
    parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json,
)


def validate_v4_artifact_evidence(
    request: ModelV4ArtifactEvidenceValidationInput,
) -> ModelV4ArtifactEvidenceValidationOutput:
    """Verify signed offline claims without accessing source, OCI, or a runtime."""

    acceptance = validate_container_bootstrap_artifact_evidence_closure_v4(
        closure=parse_container_bootstrap_artifact_evidence_closure_v4_canonical_json(
            request.closure_canonical_json
        ),
        worker_trust_policy=parse_container_bootstrap_build_worker_trust_policy_v4_canonical_json(
            request.worker_trust_policy_canonical_json
        ),
        profile_envelope=parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
            request.profile_envelope_canonical_json
        ),
        profile_trust_anchor=parse_container_bootstrap_static_profile_trust_anchor_v4_canonical_json(
            request.profile_trust_anchor_canonical_json
        ),
    )
    return ModelV4ArtifactEvidenceValidationOutput(
        closure_sha256=acceptance.closure_sha256,
        verification_context_sha256=acceptance.verification_context_sha256,
    )


class HandlerV4ArtifactEvidence:
    """Effect-free COMPUTE handler for canonical artifact-evidence verification."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self, request: ModelV4ArtifactEvidenceValidationInput
    ) -> ModelV4ArtifactEvidenceValidationOutput:
        return validate_v4_artifact_evidence(request)

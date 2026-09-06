# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure V5 worker-evidence closure validation handler."""

from __future__ import annotations

from omnimarket.nodes.node_rsd_v5_artifact_evidence_validate_compute.models.model_rsd_v5_artifact_evidence import (
    ModelRsdV5ArtifactEvidenceInput,
    ModelRsdV5ArtifactEvidenceOutput,
)
from omnimarket.rsd.container_bootstrap_artifact_evidence_v5 import (
    validate_container_bootstrap_artifact_evidence_closure_v5,
)


def validate_rsd_v5_artifact_evidence(
    request: ModelRsdV5ArtifactEvidenceInput,
) -> ModelRsdV5ArtifactEvidenceOutput:
    """Verify a supplied closure without creating authority or runtime effects."""

    acceptance = validate_container_bootstrap_artifact_evidence_closure_v5(
        closure=request.closure,
        worker_trust_policy=request.worker_trust_policy,
        profile_envelope=request.profile_envelope,
        profile_trust_anchor=request.profile_trust_anchor,
    )
    return ModelRsdV5ArtifactEvidenceOutput(
        acceptance=acceptance,
        closure_sha256=acceptance.closure_sha256,
        verification_context_sha256=acceptance.verification_context_sha256,
        schema_version=acceptance.schema_version,
    )


class HandlerRsdV5ArtifactEvidence:
    """ONEX COMPUTE handler with no I/O or authorization surface."""

    @property
    def handler_type(self) -> str:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> str:
        return "COMPUTE"

    async def handle(
        self, request: ModelRsdV5ArtifactEvidenceInput
    ) -> ModelRsdV5ArtifactEvidenceOutput:
        return validate_rsd_v5_artifact_evidence(request)


__all__ = ["HandlerRsdV5ArtifactEvidence", "validate_rsd_v5_artifact_evidence"]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure B2 V2 artifact-manifest validation handler."""

from __future__ import annotations

from omnimarket.nodes.node_rsd_target_delivery_artifact_manifest_v2_validate_compute.models.model_rsd_target_delivery_artifact_manifest_v2 import (
    ModelRsdTargetDeliveryArtifactManifestV2Input,
    ModelRsdTargetDeliveryArtifactManifestV2Output,
)
from omnimarket.rsd.target_delivery_artifact_manifest_v2 import (
    validate_target_delivery_artifact_manifest_v2,
)


def validate_rsd_target_delivery_artifact_manifest_v2(
    request: ModelRsdTargetDeliveryArtifactManifestV2Input,
) -> ModelRsdTargetDeliveryArtifactManifestV2Output:
    """Revalidate supplied evidence only; never creates delivery authority."""

    acceptance = validate_target_delivery_artifact_manifest_v2(
        delivery_map=request.delivery_map,
        static_delivery_projection=request.static_delivery_projection,
        b1_trust_policy=request.b1_trust_policy,
        manifest_trust_anchor=request.manifest_trust_anchor,
        role_inputs=request.role_inputs,
        v5_role_policy_inputs=request.v5_role_policy_inputs,
        manifest=request.manifest,
    )
    return ModelRsdTargetDeliveryArtifactManifestV2Output(
        acceptance=acceptance,
        manifest_sha256=acceptance.manifest_sha256,
        verification_context_sha256=acceptance.verification_context_sha256,
        schema_version=acceptance.schema_version,
    )


class HandlerRsdTargetDeliveryArtifactManifestV2:
    """ONEX COMPUTE handler with no I/O or authorization surface."""

    @property
    def handler_type(self) -> str:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> str:
        return "COMPUTE"

    async def handle(
        self, request: ModelRsdTargetDeliveryArtifactManifestV2Input
    ) -> ModelRsdTargetDeliveryArtifactManifestV2Output:
        return validate_rsd_target_delivery_artifact_manifest_v2(request)


__all__ = [
    "HandlerRsdTargetDeliveryArtifactManifestV2",
    "validate_rsd_target_delivery_artifact_manifest_v2",
]

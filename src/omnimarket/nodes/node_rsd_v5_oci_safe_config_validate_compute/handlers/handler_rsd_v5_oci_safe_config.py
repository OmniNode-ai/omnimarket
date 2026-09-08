# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure V5 OCI safe-config evidence validation handler."""

from __future__ import annotations

from omnimarket.nodes.node_rsd_v5_oci_safe_config_validate_compute.models.model_rsd_v5_oci_safe_config import (
    ModelRsdV5OciSafeConfigInput,
    ModelRsdV5OciSafeConfigOutput,
)
from omnimarket.rsd.container_bootstrap_oci_safe_config_evidence_v5 import (
    container_bootstrap_oci_safe_config_evidence_v5_sha256,
    verify_container_bootstrap_oci_safe_config_evidence_v5_internal_consistency,
)
from omnimarket.rsd.oci_config_commitment import (
    verify_phase_a_v5_expanded_oci_config_claim_v1,
)


def validate_rsd_v5_oci_safe_config(
    request: ModelRsdV5OciSafeConfigInput,
) -> ModelRsdV5OciSafeConfigOutput:
    """Recheck safe-config evidence against transient raw config bytes."""

    checked = (
        verify_container_bootstrap_oci_safe_config_evidence_v5_internal_consistency(
            request.evidence, request.raw_config_json
        )
    )
    claim = checked.expanded_oci_config_claim
    verify_phase_a_v5_expanded_oci_config_claim_v1(
        request.raw_config_json,
        claim.oci_config_descriptor_media_type,
        claim.oci_config_descriptor_digest,
        claim.oci_config_descriptor_size,
        claim,
    )
    return ModelRsdV5OciSafeConfigOutput(
        evidence_sha256=container_bootstrap_oci_safe_config_evidence_v5_sha256(checked),
        config_commitment_sha256=claim.oci_config_descriptor_digest.removeprefix(
            "sha256:"
        ),
        schema_version=checked.schema_version,
    )


class HandlerRsdV5OciSafeConfig:
    """ONEX COMPUTE handler with no I/O or authority surface."""

    @property
    def handler_type(self) -> str:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> str:
        return "COMPUTE"

    async def handle(
        self, request: ModelRsdV5OciSafeConfigInput
    ) -> ModelRsdV5OciSafeConfigOutput:
        return validate_rsd_v5_oci_safe_config(request)


__all__ = ["HandlerRsdV5OciSafeConfig", "validate_rsd_v5_oci_safe_config"]

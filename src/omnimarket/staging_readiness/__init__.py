# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed staging-composition contract and its pure readiness engine (OMN-15253).

Shared home for:

- ``model_staging_composition`` — the declared composition, the live snapshot,
  the def-B request, and the verdict; and
- ``engine_staging_readiness`` — the pure evaluation used by
  ``node_staging_readiness_compute`` (slice 1) and, once it exists, by the
  collect EFFECT that captures a real snapshot (slice 2).

Placed alongside ``omnimarket.parity`` and for the same reason: the evaluation
engine must not live inside one node's private package, or the collect front-end
would have to import another node's internals to reuse it.
"""

from omnimarket.staging_readiness.engine_staging_readiness import (
    ALL_CHECKS,
    evaluate_staging_readiness,
    findings_for_check,
    probe_id_for,
    snapshot_field_path_exists,
)
from omnimarket.staging_readiness.model_staging_composition import (
    EnumSecretValidationMethod,
    EnumStagingFindingSeverity,
    EnumStagingReadiness,
    EnumStagingReadinessCheck,
    EnumTopicProvisioningMode,
    ModelStagingCompositionContract,
    ModelStagingLiveSnapshot,
    ModelStagingReadinessFinding,
    ModelStagingReadinessProvenance,
    ModelStagingReadinessRequest,
    ModelStagingReadinessVerdict,
    canonical_sha256,
    document_sha256,
)

__all__ = [
    "ALL_CHECKS",
    "EnumSecretValidationMethod",
    "EnumStagingFindingSeverity",
    "EnumStagingReadiness",
    "EnumStagingReadinessCheck",
    "EnumTopicProvisioningMode",
    "ModelStagingCompositionContract",
    "ModelStagingLiveSnapshot",
    "ModelStagingReadinessFinding",
    "ModelStagingReadinessProvenance",
    "ModelStagingReadinessRequest",
    "ModelStagingReadinessVerdict",
    "canonical_sha256",
    "document_sha256",
    "evaluate_staging_readiness",
    "findings_for_check",
    "probe_id_for",
    "snapshot_field_path_exists",
]

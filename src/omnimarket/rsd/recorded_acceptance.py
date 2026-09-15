# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Offline validation of separately recorded C0 and B2 evidence.

This module deliberately does not claim that a routing decision selected a
container profile.  C0 routing evidence and B2 container-delivery evidence
currently have no shared, signed binding field.  It validates each supplied
record through its canonical validator and labels that cross-binding as
unsupported rather than manufacturing a new authority surface.

Usage: construct :class:`RecordedRsdAcceptanceInput` entirely from
caller-supplied typed records and call :func:`validate_recorded_rsd_acceptance`.
Its result always reports ``cross_binding_status="UNSUPPORTED"`` and
``live_end_to_end_acceptance=False``.  Use
:func:`require_recorded_rsd_cross_binding` only when a caller needs the
stronger claim; it fails closed until canonical routing contracts define an
explicit shared binding.  Synthetic fixtures prove parser/substitution behavior
only and are not live delegation evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from omnimarket.nodes.node_rsd_offline_c0_validate_compute.handlers.handler_rsd_offline_c0 import (
    validate_rsd_offline_c0,
)
from omnimarket.nodes.node_rsd_offline_c0_validate_compute.models.model_rsd_offline_c0 import (
    ModelRsdOfflineC0Input,
    ModelRsdOfflineC0Output,
)
from omnimarket.rsd.b1_projection_binding import (
    TargetDeliveryMapProjectionBindingTrustPolicyV1,
)
from omnimarket.rsd.historical_target_delivery_map_v1 import TargetDeliveryMapV1
from omnimarket.rsd.target_delivery_artifact_manifest_trust_anchor_v1 import (
    TargetDeliveryArtifactManifestTrustAnchorV1,
)
from omnimarket.rsd.target_delivery_artifact_manifest_v2 import (
    TargetDeliveryArtifactManifestAcceptanceV2,
    TargetDeliveryArtifactManifestRoleInputV2,
    TargetDeliveryArtifactManifestV2,
    TargetDeliveryArtifactManifestV5RolePolicyInputV2,
    validate_target_delivery_artifact_manifest_v2,
)
from omnimarket.rsd.v4_static_profile import (
    ContainerBootstrapStaticDeliveryProjectionV4,
)


class RecordedRsdCrossBindingUnsupportedError(ValueError):
    """Raised when a caller attempts to claim unavailable C0-to-B2 binding."""

    def __init__(self) -> None:
        super().__init__("recorded RSD C0-to-B2 cross-binding is unsupported")


@dataclass(frozen=True, slots=True)
class RecordedRsdAcceptanceInput:
    """Caller-supplied historical evidence only; nothing is discovered or run."""

    c0: ModelRsdOfflineC0Input
    delivery_map: TargetDeliveryMapV1
    static_delivery_projection: ContainerBootstrapStaticDeliveryProjectionV4
    b1_trust_policy: TargetDeliveryMapProjectionBindingTrustPolicyV1
    manifest_trust_anchor: TargetDeliveryArtifactManifestTrustAnchorV1
    role_inputs: tuple[
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
        TargetDeliveryArtifactManifestRoleInputV2,
    ]
    v5_role_policy_inputs: tuple[
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
        TargetDeliveryArtifactManifestV5RolePolicyInputV2,
    ]
    manifest: TargetDeliveryArtifactManifestV2


@dataclass(frozen=True, slots=True)
class RecordedRsdAcceptanceResult:
    """Two canonical validations, explicitly not a cross-bound acceptance."""

    c0: ModelRsdOfflineC0Output
    b2: TargetDeliveryArtifactManifestAcceptanceV2
    cross_binding_status: Literal["UNSUPPORTED"] = "UNSUPPORTED"
    live_end_to_end_acceptance: Literal[False] = False


def validate_recorded_rsd_acceptance(
    recorded: RecordedRsdAcceptanceInput,
) -> RecordedRsdAcceptanceResult:
    """Validate supplied C0 and B2 evidence without dispatching or authorizing.

    B2 V2 owns the B1 and V5 revalidation; this function intentionally does
    not repeat either closure.  The return type prevents consumers from
    mistaking two valid, independent evidence records for a live delegation
    acceptance or a route-to-profile binding.
    """

    if type(recorded) is not RecordedRsdAcceptanceInput:
        raise TypeError("recorded RSD acceptance input is invalid")
    c0 = validate_rsd_offline_c0(recorded.c0)
    b2 = validate_target_delivery_artifact_manifest_v2(
        delivery_map=recorded.delivery_map,
        static_delivery_projection=recorded.static_delivery_projection,
        b1_trust_policy=recorded.b1_trust_policy,
        manifest_trust_anchor=recorded.manifest_trust_anchor,
        role_inputs=recorded.role_inputs,
        v5_role_policy_inputs=recorded.v5_role_policy_inputs,
        manifest=recorded.manifest,
    )
    return RecordedRsdAcceptanceResult(c0=c0, b2=b2)


def require_recorded_rsd_cross_binding(
    recorded: RecordedRsdAcceptanceInput,
) -> None:
    """Refuse a stronger claim until a canonical signed binding exists."""

    validate_recorded_rsd_acceptance(recorded)
    raise RecordedRsdCrossBindingUnsupportedError()


__all__ = [
    "RecordedRsdAcceptanceInput",
    "RecordedRsdAcceptanceResult",
    "RecordedRsdCrossBindingUnsupportedError",
    "require_recorded_rsd_cross_binding",
    "validate_recorded_rsd_acceptance",
]

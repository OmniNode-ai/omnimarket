"""Pure verification of a supplied V4 static profile envelope."""

from typing import Literal

from omnimarket.nodes.node_rsd_v4_static_profile_validate_compute.models.model_v4_static_profile import (
    ModelV4StaticProfileValidationInput,
    ModelV4StaticProfileValidationOutput,
)
from omnimarket.rsd.v4_static_profile import (
    ContainerBootstrapStaticProfileTrustAnchorV4,
    container_bootstrap_static_role_profile_envelope_v4_sha256,
    parse_container_bootstrap_static_profile_trust_anchor_v4_canonical_json,
    parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json,
    verify_container_bootstrap_static_role_profile_envelope_v4,
)


def validate_v4_static_profile(
    request: ModelV4StaticProfileValidationInput,
) -> ModelV4StaticProfileValidationOutput:
    """Verify profile identity only; no map, ticket, replay, or runtime action exists."""

    envelope = parse_container_bootstrap_static_role_profile_envelope_v4_canonical_json(
        request.profile_envelope_canonical_json
    )
    anchor: ContainerBootstrapStaticProfileTrustAnchorV4 = (
        parse_container_bootstrap_static_profile_trust_anchor_v4_canonical_json(
            request.profile_trust_anchor_canonical_json
        )
    )
    profile = verify_container_bootstrap_static_role_profile_envelope_v4(
        envelope=envelope, profile_trust_anchor=anchor
    )
    return ModelV4StaticProfileValidationOutput(
        profile_sha256=profile.profile_sha256,
        profile_envelope_sha256=container_bootstrap_static_role_profile_envelope_v4_sha256(
            envelope
        ),
        component=profile.component,
    )


class HandlerV4StaticProfile:
    """Pure ONEX compute handler for static identity verification."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["COMPUTE"]:
        return "COMPUTE"

    async def handle(
        self, request: ModelV4StaticProfileValidationInput
    ) -> ModelV4StaticProfileValidationOutput:
        return validate_v4_static_profile(request)


__all__ = ["HandlerV4StaticProfile", "validate_v4_static_profile"]

"""Offline V4 static-profile validation node."""

from omnimarket.nodes.node_rsd_v4_static_profile_validate_compute.handlers.handler_v4_static_profile import (
    HandlerV4StaticProfile,
    validate_v4_static_profile,
)
from omnimarket.nodes.node_rsd_v4_static_profile_validate_compute.models.model_v4_static_profile import (
    ModelV4StaticProfileValidationInput,
    ModelV4StaticProfileValidationOutput,
)


class NodeRsdV4StaticProfileValidateCompute(HandlerV4StaticProfile):
    """ONEX entry-point wrapper for the effect-free V4 profile verifier."""


__all__ = [
    "HandlerV4StaticProfile",
    "ModelV4StaticProfileValidationInput",
    "ModelV4StaticProfileValidationOutput",
    "NodeRsdV4StaticProfileValidateCompute",
    "validate_v4_static_profile",
]

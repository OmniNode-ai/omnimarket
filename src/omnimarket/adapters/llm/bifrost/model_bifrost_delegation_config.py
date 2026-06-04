# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Canonical Bifrost delegation config DTOs (graduated to omnibase_core, OMN-12659)."""

from omnibase_core.models.delegation.wire import (
    ModelBifrostDelegationConfig,
    ModelDelegationBackendConfig,
    ModelDelegationCircuitBreakerConfig,
    ModelDelegationFailoverConfig,
    ModelDelegationFallbackPolicy,
    ModelDelegationRoutingRule,
    ModelDelegationShadowConfig,
)

__all__: list[str] = [
    "ModelBifrostDelegationConfig",
    "ModelDelegationBackendConfig",
    "ModelDelegationCircuitBreakerConfig",
    "ModelDelegationFailoverConfig",
    "ModelDelegationFallbackPolicy",
    "ModelDelegationRoutingRule",
    "ModelDelegationShadowConfig",
]

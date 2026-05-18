# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Compatibility imports for canonical Bifrost delegation config DTOs."""

from omnimarket.models.delegation.wire import (
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

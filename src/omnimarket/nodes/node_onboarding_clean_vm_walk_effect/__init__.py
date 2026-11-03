# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-backed clean VM participant onboarding effect."""

from omnimarket.nodes.node_onboarding_clean_vm_walk_effect.handlers.handler_onboarding_clean_vm_walk import (
    HandlerOnboardingCleanVmWalk,
)


class NodeOnboardingCleanVmWalkEffect(HandlerOnboardingCleanVmWalk):
    """ONEX entry point."""


__all__ = ["HandlerOnboardingCleanVmWalk", "NodeOnboardingCleanVmWalkEffect"]

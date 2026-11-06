# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ItemRecoveryPlan compute node."""

from .handlers.handler_item_recovery_plan import HandlerItemRecoveryPlan


class NodeItemRecoveryPlanCompute(HandlerItemRecoveryPlan):
    """Declarative runtime entry point."""

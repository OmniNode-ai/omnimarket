# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delegation_scenario_days_compute: consecutive green UTC days per delegation readiness scenario (OMN-23171)."""

from omnimarket.nodes.node_delegation_scenario_days_compute.handlers.handler_scenario_green_days import (
    HandlerScenarioGreenDays,
)

__all__ = ["HandlerScenarioGreenDays", "NodeDelegationScenarioDaysCompute"]


class NodeDelegationScenarioDaysCompute(HandlerScenarioGreenDays):
    """ONEX entry-point wrapper for HandlerScenarioGreenDays."""

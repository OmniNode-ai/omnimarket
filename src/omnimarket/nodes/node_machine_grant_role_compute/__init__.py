# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Machine grant role compute node (OMN-22139)."""

from omnimarket.nodes.node_machine_grant_role_compute.handlers.handler_machine_grant_role import (
    HandlerMachineGrantRole,
)


class NodeMachineGrantRoleCompute(HandlerMachineGrantRole):
    """ONEX entrypoint: which machine principals hold a grant role, and its grants."""


__all__ = ["HandlerMachineGrantRole", "NodeMachineGrantRoleCompute"]

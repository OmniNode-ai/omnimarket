# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B ledger friction rollup compute node (OMN-18008)."""

from omnimarket.nodes.node_friction_rollup_compute.handlers.handler_friction_rollup import (
    HandlerFrictionRollup,
)


class NodeFrictionRollupCompute(HandlerFrictionRollup):
    """ONEX entrypoint for friction rollup decisions over caller-provided texts."""


__all__ = ["HandlerFrictionRollup", "NodeFrictionRollupCompute"]

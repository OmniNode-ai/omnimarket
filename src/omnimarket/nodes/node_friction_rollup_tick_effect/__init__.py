# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Friction rollup tick effect node: the scheduled unit that moved here from the timer (OMN-22987)."""

from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.handler_friction_rollup_tick import (
    HandlerFrictionRollupTick,
)


class NodeFrictionRollupTickEffect(HandlerFrictionRollupTick):
    """ONEX entrypoint for one friction rollup tick."""


__all__ = ["HandlerFrictionRollupTick", "NodeFrictionRollupTickEffect"]

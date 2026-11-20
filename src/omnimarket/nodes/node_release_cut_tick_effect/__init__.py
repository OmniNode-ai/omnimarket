# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Release cut tick effect node: the nightly unit that cuts an omnimarket release when it is ready."""

from omnimarket.nodes.node_release_cut_tick_effect.handlers.handler_release_cut_tick import (
    HandlerReleaseCutTick,
)


class NodeReleaseCutTickEffect(HandlerReleaseCutTick):
    """ONEX entrypoint for one release cut tick."""


__all__ = ["HandlerReleaseCutTick", "NodeReleaseCutTickEffect"]

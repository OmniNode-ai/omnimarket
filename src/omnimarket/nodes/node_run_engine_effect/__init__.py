# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run engine effect node: hold the order of work and its waits outside any session, behind one seam."""

from __future__ import annotations

from omnimarket.nodes.node_run_engine_effect.handlers.handler_run_engine import (
    HandlerRunEngine,
)


class NodeRunEngineEffect(HandlerRunEngine):
    """ONEX entrypoint: the backend the node overlay selects, the in-memory one when there is no overlay."""


__all__ = ["HandlerRunEngine", "NodeRunEngineEffect"]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Envelope-shaped handlers the runtime binds: each builds the real handler when a command arrives.

CodeGraphRun and CodeGraphShadowCheck take the overlay, host, source, appender and clock as
constructor parameters, which the boot resolver cannot supply. These wrappers take none, read the
overlay and the environment at the dispatch boundary, and answer the command with the real handler.
"""

from __future__ import annotations

from omnimarket.models.model_code_graph import (
    ModelCodeGraphCommand,
    ModelCodeGraphResult,
    ModelShadowCommand,
    ModelShadowResult,
)
from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import (
    build_handler,
    build_shadow_handler,
)


class HandlerCodeGraphRequested:
    """Answer a code graph command with the nightly handler built from the overlay."""

    def handle(self, command: ModelCodeGraphCommand) -> ModelCodeGraphResult:
        handler, _ = build_handler(command.write)
        return handler.handle(command)


class HandlerCodeGraphShadowRequested:
    """Answer a shadow command with the shadow handler built from the overlay."""

    def handle(self, command: ModelShadowCommand) -> ModelShadowResult:
        handler, _ = build_shadow_handler(command.write)
        return handler.handle(command)

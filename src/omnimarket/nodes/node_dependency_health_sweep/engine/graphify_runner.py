# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GraphifyRunner — stable interface consumed by the cross-reference engine.

Delegates to GraphifyAdapter without exposing graphify or AST internals to callers.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_dependency_health_sweep.engine.graphify_adapter import (
    GraphifyAdapter,
)
from omnimarket.nodes.node_dependency_health_sweep.models.model_graph_types import (
    ModelImportGraph,
)


class GraphifyRunner:
    """Run import graph analysis over a source tree."""

    def __init__(self, adapter: GraphifyAdapter | None = None) -> None:
        self._adapter = adapter if adapter is not None else GraphifyAdapter()

    def run(self, root: Path) -> ModelImportGraph:
        """Return the import graph for the source tree at root."""
        return self._adapter.get_import_graph(root=root)

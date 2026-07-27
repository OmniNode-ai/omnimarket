# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared import-graph model.

Promoted out of node_dependency_health_sweep (OMN-14295) so
node_architecture_graph_populate_effect can reuse the same exact-resolution
AST import scanner without reaching into another node's private package
(CLAUDE.md: "Do not make one node import another node's private handler or
model package. Promote shared types instead.").
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelImportGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: list[str]
    edges: list[tuple[str, str]]
    orphan_modules: list[str]

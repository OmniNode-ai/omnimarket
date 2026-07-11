# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Graph and diff models for node_dependency_health_sweep engine."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_import_graph import ModelImportGraph
from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    ModelDepHealthFinding,
)

# OMN-14295: ModelImportGraph moved to omnimarket.models so
# node_architecture_graph_populate_effect can share it without reaching into
# this node's private package; re-exported here so every existing import of
# `model_graph_types.ModelImportGraph` keeps working unchanged.
__all__ = [
    "ModelBaselineSnapshot",
    "ModelDiffResult",
    "ModelImportGraph",
    "ModelTopologyGraph",
]


class ModelTopologyGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: list[str]
    pub_edges: list[tuple[str, str, str]]
    sub_edges: list[tuple[str, str, str]]
    orphan_topics: list[str]
    undeclared_topics: list[str]
    # Maps topic → absolute path of the contract.yaml where it was published.
    # Used by CrossReferenceEngine to populate file_path on MISSING_TOPIC_EDGE findings.
    topic_sources: dict[str, str] = {}
    # Maps topic literal → absolute path of the source file where the literal appears.
    # Used by CrossReferenceEngine to populate file_path on UNDECLARED_TOPIC findings.
    undeclared_topic_sources: dict[str, str] = {}


class ModelBaselineSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: list[ModelDepHealthFinding]
    graphify_version: str
    rule_version: str
    captured_at: datetime


class ModelDiffResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    new_findings: list[ModelDepHealthFinding]
    resolved_findings: list[ModelDepHealthFinding]
    delta: int

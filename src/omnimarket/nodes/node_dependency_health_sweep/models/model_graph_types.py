# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Graph and diff models for node_dependency_health_sweep engine."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dependency_health_sweep.models.model_dep_health_finding import (
    ModelDepHealthFinding,
)


class ModelImportGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: list[str]
    edges: list[tuple[str, str]]
    orphan_modules: list[str]


class ModelTopologyGraph(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes: list[str]
    pub_edges: list[tuple[str, str, str]]
    sub_edges: list[tuple[str, str, str]]
    orphan_topics: list[str]
    undeclared_topics: list[str]


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

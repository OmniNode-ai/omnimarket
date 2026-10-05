# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input of the projection contract checks: explicit text, no filesystem."""

from omnibase_core.models.nodes.no_utcnow_check.model_source_file import ModelSourceFile
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_contract_projection_check_compute.models.enum_projection_contract_rule import (
    EnumProjectionContractRule,
)
from omnimarket.nodes.node_contract_projection_check_compute.models.model_projection_node_sources import (
    ModelProjectionNodeSources,
)


class ModelProjectionContractCheckInput(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rule: EnumProjectionContractRule
    nodes: tuple[ModelProjectionNodeSources, ...] = Field(
        default=(), description="Used by the access and cursor rules."
    )
    handlers: tuple[ModelSourceFile, ...] = Field(
        default=(), description="Used by the dlq rule: projection handler modules."
    )
    cursor_baseline: tuple[str, ...] = Field(
        default=(),
        description="Frozen shrink-only baseline of exposure ids without a cursor_column.",
    )
    require_scanned: bool = Field(
        default=True,
        description="A full-tree run that gathered nothing is an ERROR, never a PASS.",
    )

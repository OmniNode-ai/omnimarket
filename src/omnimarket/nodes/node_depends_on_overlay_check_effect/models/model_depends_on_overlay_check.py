# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the depends-on overlay check node (OMN-24537): the command, a finding, and the result."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_depends_on_overlay_check_effect.models.enum_depends_on_overlay_check import (
    EnumDependsOnFindingCode,
)


class ModelDependsOnOverlayCheckCommand(BaseModel):
    """Where the node contracts and the overlays are, how each is named, and which nodes to judge."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nodes_root: str = Field(
        min_length=1, description="Directory holding one directory per node"
    )
    overlays_root: str = Field(
        min_length=1, description="Overlays directory: one directory per overlay id"
    )
    contract_file_name: str = Field(
        min_length=1, description="A node's contract file name"
    )
    overlay_file_name: str = Field(
        min_length=1, description="An overlay document's file name"
    )
    overlay_kind: str = Field(
        min_length=1,
        description="The dependency kind the overlay facts are supplied under",
    )
    node_names: tuple[str, ...] = Field(
        default=(),
        description="Nodes to judge; empty judges every node under the nodes root",
    )


class ModelDependsOnFinding(BaseModel):
    """One refusal of the check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    code: EnumDependsOnFindingCode
    detail: str
    line: int | None = None


class ModelDependsOnOverlayCheckResult(BaseModel):
    """What was judged and what was refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    nodes_checked: int
    dependencies_checked: int
    overlays_present: int
    findings: tuple[ModelDependsOnFinding, ...]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One node package's contract text and production handler modules, read at the boundary."""

from omnibase_core.models.nodes.no_utcnow_check.model_source_file import ModelSourceFile
from pydantic import BaseModel, ConfigDict, Field


class ModelProjectionNodeSources(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    node: str = Field(min_length=1, description="Node package directory name.")
    contract_path: str = Field(description="Repo-relative path of the contract.yaml.")
    contract_text: str = Field(description="Raw contract.yaml text.")
    modules: tuple[ModelSourceFile, ...] = Field(
        default=(),
        description="Production handler modules of the node (tests excluded), repo-relative paths.",
    )

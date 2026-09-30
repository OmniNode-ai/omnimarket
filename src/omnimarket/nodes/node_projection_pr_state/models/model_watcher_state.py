# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The actual schema-1 watcher file layout."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_pr_state.models.model_watcher_pr import (
    ModelWatcherPr,
)


class ModelWatcherState(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="ignore")
    schema_version: Literal[1] = Field(alias="schema")
    prs: dict[str, ModelWatcherPr]

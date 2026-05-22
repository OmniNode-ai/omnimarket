# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for node_chain_diff."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_chain_diff.models.model_golden_chain_entry import (
    ModelGoldenChainEntry,
)

__all__ = ["ModelChainDiffRequest"]


class ModelChainDiffRequest(BaseModel):
    """Input for the chain diff compute node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected: tuple[ModelGoldenChainEntry, ...]
    observed: tuple[ModelGoldenChainEntry, ...]

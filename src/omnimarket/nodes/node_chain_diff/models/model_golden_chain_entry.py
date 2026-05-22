# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain entry — local copy until omnibase_core PR #1129 (OMN-11675) merges."""

# TODO(OMN-11675): replace with: from omnibase_core.models.pipeline.model_golden_chain_entry import ModelGoldenChainEntry

from pydantic import BaseModel, ConfigDict

__all__ = ["ModelGoldenChainEntry"]


class ModelGoldenChainEntry(BaseModel):
    """A single event in a golden (expected) chain sequence."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    sequence: int
    event_type: str
    topic: str
    source_node: str

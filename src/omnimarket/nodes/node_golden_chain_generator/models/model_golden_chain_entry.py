# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain entry — a single event in an expected event chain.

Local copy until omnibase_core graduates this model from OMN-11675.
Once that PR merges, imports should move to
omnibase_core.models.pipeline.ModelGoldenChainEntry.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["ModelGoldenChainEntry"]


class ModelGoldenChainEntry(BaseModel):
    """A single event in a golden (expected) chain sequence."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    sequence: int
    event_type: str
    topic: str
    source_node: str

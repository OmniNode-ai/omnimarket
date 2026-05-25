# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Validity scope model constraining when a context pack is applicable (OMN-12034)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelContextPackValidityScope(BaseModel):
    """Constraints under which a ModelContextPackExtended is applicable.

    All fields are descriptive; enforcement is the caller's responsibility.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    model_id: str = Field(min_length=1)
    harness_kind: str = Field(min_length=1)
    execution_mode: str = Field(min_length=1)
    task_class: str = Field(min_length=1)
    topology_class: str = Field(min_length=1)


__all__ = ["ModelContextPackValidityScope"]

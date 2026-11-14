# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed config, input and output of the entry-point registration verdict (OMN-24354).

Frozen Pydantic models reject undeclared fields.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class ModelReviewedUnregisteredNode(BaseModel):
    """One reviewed node directory that may have no onex.nodes entry point."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    node: str
    reason: str

    @field_validator("node", "reason")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a reviewed entry needs a non-empty node and reason")
        return value


class ModelEntryPointRegistrationConfig(BaseModel):
    """The contract's reviewed list: each node with the reason it has no entry point."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    reviewed_unregistered_nodes: tuple[ModelReviewedUnregisteredNode, ...]

    @model_validator(mode="after")
    def _no_duplicates(self) -> ModelEntryPointRegistrationConfig:
        seen: set[str] = set()
        for entry in self.reviewed_unregistered_nodes:
            if entry.node in seen:
                raise ValueError(f"{entry.node} is listed twice in the reviewed list")
            seen.add(entry.node)
        return self


class ModelEntryPointRegistrationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    node_name: str
    # the names registered under [project.entry-points."onex.nodes"]
    entry_points: frozenset[str]


class ModelEntryPointRegistrationVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    node_name: str
    registered: bool
    # the reviewed reason when the node is on the list, else None
    reviewed_reason: str | None
    passes: bool

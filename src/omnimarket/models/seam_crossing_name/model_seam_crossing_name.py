# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the seam crossing name compute node."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.seam_field_diff import ModelSeamModelSummary
from omnimarket.nodes.node_seam_crossing_name_compute.models.model_seam_crossing_naming import (
    ModelSeamCrossingNaming,
)
from omnimarket.nodes.node_seam_crossing_name_compute.overlay import (
    load_seam_crossing_naming,
)


class EnumSeamCrossingDecidedBy(StrEnum):
    RULE_R = "rule_r"
    KEY_CHECK = "key_check"


class ModelSeamCrossing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    crossing_id: str = Field(min_length=1)
    seam_id: str
    topic: str
    consumer: ModelSeamModelSummary
    body_keys: tuple[str, ...]


class ModelSeamCrossingNameRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    crossings: tuple[ModelSeamCrossing, ...]
    naming: ModelSeamCrossingNaming = Field(default_factory=load_seam_crossing_naming)

    @field_validator("crossings")
    @classmethod
    def _no_duplicate_crossing_ids(
        cls, v: tuple[ModelSeamCrossing, ...]
    ) -> tuple[ModelSeamCrossing, ...]:
        seen: set[str] = set()
        for c in v:
            if c.crossing_id in seen:
                raise ValueError(f"duplicate crossing_id: {c.crossing_id}")
            seen.add(c.crossing_id)
        return v


class ModelSeamCrossingName(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    crossing_id: str
    seam_id: str
    topic: str
    name: str
    fields: tuple[str, ...]
    decided_by: EnumSeamCrossingDecidedBy
    # The consumer has an adapter that may supply or reshape fields, so the name
    # rests on declared fields and is advisory.
    consumer_has_adapter: bool


class ModelSeamCrossingNameVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    names: tuple[ModelSeamCrossingName, ...]
    counts: dict[str, int]
    # Count of names whose consumer has an adapter.
    adapter_advisory: int
    # Non-None routes the result to the contract's failure terminal (the runtime's
    # failure-terminal guard reads this field). The naming handler never sets it:
    # a malformed request fails at validation, before the handler runs.
    terminal_failure_cause: str | None = None

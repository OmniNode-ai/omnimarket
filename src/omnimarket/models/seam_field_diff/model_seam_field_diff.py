# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the seam field diff compute node (OMN-24121)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnumSeamFieldIssue(StrEnum):
    MISSING_FIELD = "missing_field"
    TYPE_MISMATCH = "type_mismatch"
    OPTIONAL_VS_REQUIRED = "optional_vs_required"
    EXTRA_FIELD_REJECTED = "extra_field_rejected"


class ModelSeamFieldFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    type_name: str
    required: bool
    nullable: bool
    has_default: bool


class ModelSeamModelSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    model_ref: str
    fields: dict[str, ModelSeamFieldFacts]
    nested: dict[str, dict[str, ModelSeamFieldFacts]] = {}
    extra_forbidden: bool
    has_adapter: bool


class ModelSeamPairFacts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    topic: str
    producer_contract: str
    consumer_contract: str
    producer_rule: str
    consumer_rule: str
    producer: ModelSeamModelSummary
    consumer: ModelSeamModelSummary

    @property
    def pair_key(self) -> str:
        return (
            f"{self.topic}|{self.producer_contract}|"
            f"{self.consumer_contract}|"
            f"{self.producer.model_ref}|{self.consumer.model_ref}"
        )


class ModelSeamFieldFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    pair_key: str
    topic: str
    issue: EnumSeamFieldIssue
    field: str
    detail: str

    @property
    def key(self) -> str:
        return f"{self.pair_key}#{self.issue.value}#{self.field}"


class ModelSeamFieldDiffRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    head_pairs: tuple[ModelSeamPairFacts, ...]
    base_finding_keys: tuple[str, ...]
    nested_depth_limit: int = Field(ge=1)


class ModelSeamFieldDiffVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["clean", "refused"]
    pairs_compared: int
    adapter_skipped: tuple[str, ...]
    findings: tuple[ModelSeamFieldFinding, ...]
    new_findings: tuple[ModelSeamFieldFinding, ...]

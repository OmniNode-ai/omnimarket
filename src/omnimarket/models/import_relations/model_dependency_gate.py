# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The frozen cross-dependency baseline, the gate request and the gate verdict (OMN-23136).

The baseline freezes the corrected counts of the plan's section 4.1: the number of subsystems
in the one component, and each relation of the cycle cut with its occurrence count. It carries
the sha256 of its own content, so a changed number reads as a changed file. The gate takes the
import relation report of a run and the baseline and reads RED when the count rose above it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.import_relations.model_import_relations import (
    ModelImportRelationReport,
    ModelRelationKey,
)


class EnumDependencyRiseKind(StrEnum):
    """Why the gate reads RED."""

    BASELINE_HASH_MISMATCH = "baseline-hash-mismatch"
    RELATION_ROSE = "relation-rose"
    RESIDUAL_BACK_EDGE = "residual-back-edge"
    SUBSYSTEMS_ROSE = "subsystems-rose"
    COUNT_ROSE = "count-rose"


class ModelFrozenRelation(BaseModel):
    """One relation of the frozen cut and its occurrence count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: ModelRelationKey
    occurrences: int = Field(ge=0)


class ModelDependencyBaseline(BaseModel):
    """The corrected baseline, with the hash of its own content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(min_length=1)
    frozen_at: datetime
    source: str = Field(min_length=1)
    subsystems: int = Field(ge=1)
    relations: tuple[ModelFrozenRelation, ...] = Field(min_length=1)
    total_occurrences: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _total_is_the_sum(self) -> ModelDependencyBaseline:
        if self.total_occurrences != sum(r.occurrences for r in self.relations):
            raise ValueError("total_occurrences is not the sum of the relations")
        return self

    def body_digest(self) -> str:
        """sha256 over every field but ``sha256``, as sorted compact JSON."""
        body = self.model_dump(mode="json", exclude={"sha256"})
        text = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def hash_matches(self) -> bool:
        return self.sha256 == self.body_digest()

    @classmethod
    def sealed(
        cls,
        *,
        schema_version: str,
        frozen_at: datetime,
        source: str,
        subsystems: int,
        relations: tuple[ModelFrozenRelation, ...],
    ) -> ModelDependencyBaseline:
        """Build the baseline with its hash filled in from its own content."""
        unsealed = cls(
            schema_version=schema_version,
            frozen_at=frozen_at,
            source=source,
            subsystems=subsystems,
            relations=relations,
            total_occurrences=sum(r.occurrences for r in relations),
            sha256="0" * 64,
        )
        return unsealed.model_copy(update={"sha256": unsealed.body_digest()})


class ModelDependencyGateRequest(BaseModel):
    """The run's import relation report and the baseline the caller read from its file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    report: ModelImportRelationReport
    baseline: ModelDependencyBaseline


class ModelDependencyRise(BaseModel):
    """One reason the count is above the frozen value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumDependencyRiseKind
    detail: str
    frozen: int | None = None
    current: int | None = None


class ModelDependencyGateResult(BaseModel):
    """The gate's verdict and both counts, so a reader shows the current and the frozen value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    state: Literal["PASS", "RED"]
    #: Occurrences of the frozen cut's relations now, plus the residual back edges.
    current_count: int = Field(ge=0)
    frozen_count: int = Field(ge=0)
    #: Back-edge occurrences left in the graph once the frozen cut's relations are removed.
    residual_occurrences: int = Field(ge=0)
    current_subsystems: int = Field(ge=0)
    frozen_subsystems: int = Field(ge=1)
    baseline_sha256: str
    baseline_intact: bool
    rises: tuple[ModelDependencyRise, ...]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, overlay and report models of the directed import relations.

The graph's import edges go in, grouped under labels by the overlay's rules (a repository
is its own label unless a rule says otherwise). The report keeps every direction of every
label pair as its own relation, names each cycle among the labels, and maps each tracked
relation to the joins of the pieces that own its two endpoints. Every list is sorted, so
one request always gives one set of bytes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.import_relations.enum_import_relations import (
    EnumRelationFindingKind,
)


class ModelImportEdge(BaseModel):
    """One import occurrence: the importing file and the file it lands on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    importer_repo: str = Field(min_length=1)
    importer_path: str = Field(min_length=1)
    imported_repo: str = Field(min_length=1)
    imported_path: str = Field(min_length=1)


class ModelRelationKey(BaseModel):
    """A directed relation between two labels: ``source`` imports ``target``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)

    def text(self) -> str:
        return f"{self.source} to {self.target}"


class ModelLabelRule(BaseModel):
    """Files of ``repo`` whose path starts with ``path_prefix`` carry ``label``; first rule wins."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    path_prefix: str


class ModelLabelOwner(BaseModel):
    """The piece that owns a label."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1)
    piece: int = Field(ge=1)


class ModelJoinDef(BaseModel):
    """One join of the plan and the pieces at its two ends."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    join_id: str = Field(min_length=1)
    pieces: tuple[int, ...] = Field(min_length=1)


class ModelImportRelationOverlay(BaseModel):
    """The values the node is handed; none is held in its code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label_rules: tuple[ModelLabelRule, ...]
    tracked_relations: tuple[ModelRelationKey, ...]
    label_owners: tuple[ModelLabelOwner, ...]
    joins: tuple[ModelJoinDef, ...]


class ModelImportRelationRequest(BaseModel):
    """The import edges, the overlay and the reference time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edges: tuple[ModelImportEdge, ...]
    overlay: ModelImportRelationOverlay
    reference_time: datetime
    #: Injected break: keep only the stronger direction of each label pair, the way the old
    #: graph did. A run with it set must read no cycle; the control that shows the check is live.
    keep_stronger_direction_only: bool = False


class ModelRelation(BaseModel):
    """One directed relation and its counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: ModelRelationKey
    #: Distinct (importing file, imported file) pairs.
    occurrences: int = Field(ge=0)
    #: Distinct files on either end.
    entities: int = Field(ge=0)
    #: Distinct importing files.
    files: int = Field(ge=0)


class ModelRelationCycle(BaseModel):
    """One strongly connected set of labels (two or more) and the relations inside it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    labels: tuple[str, ...]
    relations: tuple[ModelRelationKey, ...]
    occurrences: int = Field(ge=0)


class ModelRelationFinding(BaseModel):
    """A tracked relation that cannot be read as mapped, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: ModelRelationKey
    kind: EnumRelationFindingKind
    detail: str


class ModelRelationJoinEntry(BaseModel):
    """A tracked relation with the owning piece and joins of each endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: ModelRelationKey
    occurrences: int = Field(ge=0)
    source_piece: int | None
    target_piece: int | None
    source_joins: tuple[str, ...]
    target_joins: tuple[str, ...]
    mapped: bool


class ModelImportRelationReport(BaseModel):
    """The relations, the cycles, the join map of the tracked relations and the findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    relations: tuple[ModelRelation, ...]
    cycles: tuple[ModelRelationCycle, ...]
    relation_map: tuple[ModelRelationJoinEntry, ...]
    findings: tuple[ModelRelationFinding, ...]

    @property
    def tracked_total(self) -> int:
        return len(self.relation_map)

    @property
    def tracked_mapped(self) -> int:
        return sum(1 for entry in self.relation_map if entry.mapped)

    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()

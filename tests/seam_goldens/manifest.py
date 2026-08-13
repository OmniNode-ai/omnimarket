# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed loader for the OMN-16004 frozen slice manifest.

``slice_manifest.yaml`` is the committed, inspectable enumeration of the
Milestone-B traversed slice. This module is the only code that understands its
schema: every golden imports :func:`slice_edge` to fetch its own row, so a
golden can never silently disagree with the frozen slice about which edge it
covers or what the registry says about it.

The models are ``extra="forbid"`` and ``frozen=True`` on purpose — a typo'd or
invented manifest key fails at load time, in every golden at once, rather than
being ignored.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "SLICE_MANIFEST_PATH",
    "EnumSliceInclusion",
    "ModelSliceEdge",
    "ModelSliceManifest",
    "ModelSliceRegistryBinding",
    "load_slice_manifest",
    "slice_edge",
]

SLICE_MANIFEST_PATH = Path(__file__).parent / "slice_manifest.yaml"

# Repo root, resolved relative to this file rather than a hardcoded absolute
# path so the goldens run identically from any checkout or worktree.
REPO_ROOT = Path(__file__).resolve().parents[2]


class EnumSliceInclusion(StrEnum):
    """Why an edge is (or is not) in the frozen slice."""

    CORE_ROUND_TRIP = "core_round_trip"
    WS7_MANDATORY_HIGH = "ws7_mandatory_high"
    LOCAL_PROCESSING = "local_processing"
    FLAGGED_AMBIGUOUS = "flagged_ambiguous"
    EXCLUDED = "excluded"


class ModelSliceRegistryBinding(BaseModel):
    """The registry of record this slice is frozen against.

    Every field here is asserted against the live ``seams.v1.yaml`` header by
    ``test_manifest_registry_binding``. In particular ``source_path`` pins the
    2026-08-13 re-derivation: a registry regenerated from the superseded
    2026-08-08 source reproduces the exact staleness defect the slice exists
    to guard, so it must fail closed rather than merely warn.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    generated_at: str = Field(min_length=1)
    edges_total: int = Field(gt=0)

    @property
    def resolved_path(self) -> Path:
        return REPO_ROOT / self.path


class ModelSliceEdge(BaseModel):
    """One frozen row of the traversed-slice enumeration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    edge_id: str = Field(min_length=1)
    seam: str = Field(min_length=1)
    leg: str = Field(min_length=1)
    inclusion: EnumSliceInclusion
    traversed: bool
    registry_classification: str = Field(min_length=1)
    registry_severity: str = Field(min_length=1)
    producer_symbol_reachable: bool
    golden_module: str | None = None
    rationale: str | None = None
    exclusion_reason: str | None = None

    @property
    def is_excluded(self) -> bool:
        return self.inclusion is EnumSliceInclusion.EXCLUDED

    @property
    def resolved_golden_module(self) -> Path | None:
        if self.golden_module is None:
            return None
        return REPO_ROOT / self.golden_module

    @model_validator(mode="after")
    def _validate_inclusion_consistency(self) -> ModelSliceEdge:
        """Structurally enforce the scope bound.

        An excluded edge must state WHY and must carry no golden — that pairing
        is what keeps this narrow slice from quietly growing into the full
        15-edge program. An included edge must carry both a rationale and the
        golden that actually executes it, so "enumerated" can never drift from
        "goldened".
        """

        if self.is_excluded:
            if self.golden_module is not None:
                raise ValueError(
                    f"{self.edge_id}: excluded edges must not declare a golden_module"
                )
            if not self.exclusion_reason:
                raise ValueError(
                    f"{self.edge_id}: excluded edges must state an exclusion_reason"
                )
            return self

        if self.golden_module is None:
            raise ValueError(f"{self.edge_id}: included edges require a golden_module")
        if not self.rationale:
            raise ValueError(f"{self.edge_id}: included edges require a rationale")
        if self.exclusion_reason:
            raise ValueError(
                f"{self.edge_id}: exclusion_reason is only valid on excluded edges"
            )
        return self


class ModelSliceManifest(BaseModel):
    """The whole frozen slice."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(min_length=1)
    ticket: str = Field(pattern=r"^OMN-\d+$")
    registry: ModelSliceRegistryBinding
    edges: tuple[ModelSliceEdge, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_unique_edge_ids(self) -> ModelSliceManifest:
        seen: set[str] = set()
        for edge in self.edges:
            if edge.edge_id in seen:
                raise ValueError(f"duplicate edge_id in slice manifest: {edge.edge_id}")
            seen.add(edge.edge_id)
        return self

    def by_id(self, edge_id: str) -> ModelSliceEdge:
        for edge in self.edges:
            if edge.edge_id == edge_id:
                return edge
        raise KeyError(f"edge_id not present in the frozen slice manifest: {edge_id}")

    def included(self) -> tuple[ModelSliceEdge, ...]:
        return tuple(edge for edge in self.edges if not edge.is_excluded)

    def excluded(self) -> tuple[ModelSliceEdge, ...]:
        return tuple(edge for edge in self.edges if edge.is_excluded)

    def traversed(self) -> tuple[ModelSliceEdge, ...]:
        return tuple(edge for edge in self.edges if edge.traversed)


@lru_cache(maxsize=1)
def load_slice_manifest() -> ModelSliceManifest:
    """Parse and validate ``slice_manifest.yaml``."""

    raw = yaml.safe_load(SLICE_MANIFEST_PATH.read_text(encoding="utf-8"))
    return ModelSliceManifest.model_validate(raw)


def slice_edge(edge_id: str) -> ModelSliceEdge:
    """Fetch one frozen row; raises if the golden names an edge not in slice."""

    return load_slice_manifest().by_id(edge_id)

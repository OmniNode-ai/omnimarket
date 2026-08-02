# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load and validate the static module adjacency map."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelAdjacencyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    reverse_deps: list[str] = Field(default_factory=list)


class ModelThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    modules_changed_for_full_suite: int = Field(..., ge=1)


class ModelAdjacencyMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(..., ge=1)
    shared_modules: list[str]
    thresholds: ModelThresholds
    test_infrastructure_paths: list[str]
    adjacency: dict[str, ModelAdjacencyEntry]
    always_selected_paths: list[str] = Field(default_factory=list)
    """Test directories appended to EVERY narrowed selection.

    OMN-15639: a repo-wide gate (``tests/gates/``) asserts an invariant over
    files the adjacency map cannot attribute to one module -- e.g. the
    consumer-group declaration strip walks all 384 ``src/**/contract.yaml``.
    Under narrowing, changing one node's ``contract.yaml`` resolves to module
    ``nodes`` and selects only ``tests/nodes/``, so the repo-wide gate never
    runs and the invariant it protects is silently reintroducible on the
    everyday dev path. Entries here are unioned in after narrowing and are
    deliberately NOT filtered against the on-disk tree: a missing directory
    must surface as a loud pytest collection error, never as a silent drop
    that turns the gate vacuous.
    """

    @model_validator(mode="after")
    def validate_always_selected_paths(self) -> ModelAdjacencyMap:
        for path in self.always_selected_paths:
            if not path.startswith("tests/") or not path.endswith("/"):
                raise ValueError(
                    f"always_selected_paths entry '{path}' must be a directory "
                    "under tests/ written with a trailing slash"
                )
        return self

    @model_validator(mode="after")
    def validate_shared_modules_in_adjacency(self) -> ModelAdjacencyMap:
        for shared in self.shared_modules:
            if shared not in self.adjacency:
                raise ValueError(f"shared_module '{shared}' has no adjacency entry")
        for module, entry in self.adjacency.items():
            for dep in entry.reverse_deps:
                if dep not in self.adjacency:
                    raise ValueError(
                        f"adjacency['{module}'].reverse_deps references unknown module '{dep}'"
                    )
        return self


def load_adjacency_map(path: Path) -> ModelAdjacencyMap:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelAdjacencyMap.model_validate(raw)

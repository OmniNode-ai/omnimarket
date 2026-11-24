# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of the file-level closure operation: changed files in, test paths or an escalation out."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyMap,
    ModelDataFileChange,
)


class EnumRootConftestEdge(StrEnum):
    #: A closure that reaches the root ``tests/conftest.py`` selects the tree.
    KEEP = "keep"
    #: That edge is cut: the closure's own tests run, and the whole-tree collection gate runs once.
    CUT = "cut"


class EnumClosureEscalation(StrEnum):
    #: A changed source file the closure cannot place in the import graph.
    UNRESOLVED_SOURCE = "unresolved_source"
    #: A declared project or lock file change the section rule cannot narrow (or the closure rule
    #: sends to the full suite): the same answer the section rule gives below the closure stage.
    TEST_INFRASTRUCTURE = "test_infrastructure"


class ModelFileLevelClosureRequest(BaseModel):
    """The changed files, the adjacency map (its data-file rule), the checkout and the D1 setting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    changed_files: tuple[str, ...]
    adjacency: ModelAdjacencyMap
    repo_root: str = Field(min_length=1)
    root_conftest_edge: EnumRootConftestEdge
    #: The changed keys of each declared project or lock file, read against the base ref (empty: not read).
    data_file_changes: tuple[ModelDataFileChange, ...] = ()


class ModelFileLevelClosureResult(BaseModel):
    """Test paths of the reverse import closure, or the escalation the closure could not avoid."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_paths: tuple[str, ...] = ()
    data_only: bool = False
    escalation: EnumClosureEscalation | None = None
    #: The changed files behind an escalation, in the order given.
    unresolved_files: tuple[str, ...] = ()
    #: True when the root conftest edge is cut and the closure reached ``tests/conftest.py``: the
    #: whole-tree ``pytest --collect-only`` gate then covers what the tree would have.
    collection_gate: bool = False

    @model_validator(mode="after")
    def _escalation_names_its_files(self) -> Self:
        if (self.escalation is None) != (not self.unresolved_files):
            raise ValueError(
                "unresolved_files are set exactly when the closure escalates"
            )
        return self

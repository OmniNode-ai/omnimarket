# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed roots, install-only declarations and preservation receipt."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelInstallOnlyOverlay(BaseModel):
    """Node directories relative to both install roots; no host path defaults."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    nodes: tuple[str, ...] = Field(min_length=1)

    @field_validator("nodes")
    @classmethod
    def relative_node_directories(cls, nodes: tuple[str, ...]) -> tuple[str, ...]:
        for node in nodes:
            path = PurePosixPath(node)
            if (
                path.is_absolute()
                or len(path.parts) < 2
                or any(part in {"", ".", ".."} for part in node.split("/"))
                or "\\" in node
            ):
                raise ValueError(
                    "nodes must name relative node directories without traversal"
                )
        if len(set(nodes)) != len(nodes):
            raise ValueError("nodes must be unique")
        paths = tuple(PurePosixPath(node) for node in nodes)
        if any(a != b and a in b.parents for a in paths for b in paths):
            raise ValueError("declared node directories must not overlap")
        return nodes


class ModelInstallOnlyRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    live_root: Path
    stage_root: Path
    overlay: ModelInstallOnlyOverlay | None = None


class ModelInstallOnlyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    preserved: tuple[str, ...] = ()
    stage_present: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()

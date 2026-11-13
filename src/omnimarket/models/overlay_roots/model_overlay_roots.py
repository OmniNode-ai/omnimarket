# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the overlay root resolver (OMN-24032)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumOverlayRootSource(StrEnum):
    STORE = "store"
    CANONICAL_CLONE = "canonical_clone"
    DECLARED = "declared"


class ModelOverlayRootsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    declared_roots: tuple[str, ...] = ()
    registry_root: str | None = None
    home: str | None = None


class ModelOverlayRootProbe(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    source: EnumOverlayRootSource
    present: bool


class ModelOverlayRootsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    roots: tuple[ModelOverlayRootProbe, ...]
    considered: tuple[ModelOverlayRootProbe, ...]
    store_named_by: str | None
    finding: str | None

    def paths(self) -> tuple[str, ...]:
        return tuple(root.path for root in self.roots)

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure install-confirm request assembly inputs and result (OMN-21336, TA.13)."""

from __future__ import annotations

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.context_install_confirm import (
    ModelInstallConfirmRequest,
    ModelPublishedActivation,
)


class ModelCapturedCompactChunk(BaseModel):
    """The compact content columns needed to reconstruct one captured package."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    chunk_index: int = Field(ge=0)
    chunk_count: int = Field(ge=1)
    content: str
    content_sha256: str | None
    truncated: bool = False
    emitted_at: datetime

    @model_validator(mode="after")
    def _index_within_package(self) -> Self:
        if self.chunk_index >= self.chunk_count:
            raise ValueError("chunk_index must be less than chunk_count")
        return self


class ModelInstallConfirmAssemblyRequest(BaseModel):
    """Captured chunks and published activations belonging to one session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    chunks: tuple[ModelCapturedCompactChunk, ...] = ()
    activations: tuple[ModelPublishedActivation, ...] = ()
    newest_captured_at: datetime | None = None


class ModelInstallConfirmAssemblyResult(BaseModel):
    """The boundary algorithm's request and a sorted inventory of unreadable packages."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request: ModelInstallConfirmRequest
    packages_read: int
    packages_unreadable: tuple[str, ...]


__all__ = [
    "ModelCapturedCompactChunk",
    "ModelInstallConfirmAssemblyRequest",
    "ModelInstallConfirmAssemblyResult",
]

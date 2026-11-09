# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool a projection writer keeps, as its node contract declares it (OMN-23257)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelWriterPoolSettings(BaseModel):
    """One writer's pool sizes and waits, read from its ``contract.yaml`` ``writer_pool``.

    No field has a default: a writer whose contract declares no pool stops at
    composition instead of opening one on a size typed into the code.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_size: int = Field(ge=1, description="Connections the pool holds open.")
    max_size: int = Field(
        ge=1,
        description="Most connections, and so most messages written at once.",
    )
    acquire_timeout_seconds: float = Field(
        gt=0,
        description="How long a message waits for a free connection before the pool reads as exhausted.",
    )
    command_timeout_seconds: float = Field(gt=0, description="Bound on one statement.")

    @model_validator(mode="after")
    def _max_not_below_min(self) -> ModelWriterPoolSettings:
        if self.max_size < self.min_size:
            raise ValueError("writer_pool max_size must not be below min_size")
        return self


__all__ = ["ModelWriterPoolSettings"]

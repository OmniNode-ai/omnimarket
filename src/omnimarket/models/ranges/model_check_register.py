# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check register document."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.ranges.model_check_declaration import ModelCheckDeclaration


class ModelCheckRegister(BaseModel):
    """Every registered check with its class. Never empty, never a duplicate id."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["check_register.v1"]
    checks: tuple[ModelCheckDeclaration, ...] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        seen: set[str] = set()
        for check in self.checks:
            if check.check_id in seen:
                raise ValueError(f"duplicate check_id {check.check_id!r}")
            seen.add(check.check_id)
        return self

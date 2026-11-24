# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reads a diff adds and the ones no overlay declares."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .model_undeclared_read import ModelRead


class ModelUndeclaredReadCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reads: tuple[ModelRead, ...]
    undeclared: tuple[ModelRead, ...]
    register_present: bool

    @property
    def refused(self) -> bool:
        return bool(self.undeclared)


__all__ = ["ModelUndeclaredReadCheckResult"]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One read of a setting that a diff adds."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumReadKind(StrEnum):
    ENV = "env"
    REPO_VARIABLE = "repo_variable"
    BUILD_ARG = "build_arg"


class ModelRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    line: int
    name: str
    kind: EnumReadKind
    text: str


__all__ = ["EnumReadKind", "ModelRead"]

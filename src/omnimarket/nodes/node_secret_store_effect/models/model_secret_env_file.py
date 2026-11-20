# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire models for a declared secret-store environment file."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelSecretEnvFileRequest(BaseModel):
    """Select an overlay binding and optionally confirm its declared names."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    operation: Literal["write_env_file"] = "write_env_file"
    overlay_key: str = Field(min_length=1)
    folder: str = Field(min_length=1)
    names: tuple[str, ...] | None = Field(default=None, min_length=1)
    correlation_id: UUID | None = None


class ModelSecretEnvFileReading(BaseModel):
    """The only information about one secret that leaves the handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    present: bool
    sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")


class ModelSecretEnvFileResult(BaseModel):
    """A whole-file write outcome and its secret readings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["written", "refused", "missing", "failed"]
    secrets: tuple[ModelSecretEnvFileReading, ...] = ()
    correlation_id: UUID | None = None

    @property
    def ok(self) -> bool:
        return self.status == "written"

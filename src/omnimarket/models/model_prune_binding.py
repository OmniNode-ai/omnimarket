# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Archive and database configuration shared by the retention effects."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

PruneKind = Literal["consumer_flow", "dead_letter"]


class ModelPruneBinding(BaseModel):
    """Unconfigured bindings are valid declarations, refused before any effects.

    Database credentials may be supplied by a protected overlay or referenced
    in the lane's secret store. Values never come from ambient database or
    archive environment variables.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    archive_dir: Path | None = None
    database_url: SecretStr | None = None
    database_secret_ref: str | None = None

    @field_validator("archive_dir")
    @classmethod
    def _absolute_archive_dir(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("archive_dir must be an absolute path")
        return value

    @field_validator("database_url")
    @classmethod
    def _nonempty_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("database_url must be nonempty")
        return value

    @field_validator("database_secret_ref")
    @classmethod
    def _nonempty_ref(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("database_secret_ref must be nonempty")
        return value

    @model_validator(mode="after")
    def _one_database_source(self) -> ModelPruneBinding:
        if self.database_url is not None and self.database_secret_ref is not None:
            raise ValueError("declare only one database_url or database_secret_ref")
        return self


class ModelPruneBindingRequest(BaseModel):
    """Resolve a declared binding, optionally including its database credential."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    binding: ModelPruneBinding
    kind: PruneKind
    resolve_database_url: bool = False


class ModelPruneBindingResult(BaseModel):
    """The overlay-resolved binding and, when requested, a redacted credential."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    binding: ModelPruneBinding
    database_url: SecretStr | None = None

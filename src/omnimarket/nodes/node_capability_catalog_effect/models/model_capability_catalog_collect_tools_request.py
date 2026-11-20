# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host-local, serializable inputs to the tool collector"""

from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelCapabilityCatalogCollectToolsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["collect_tools"] = "collect_tools"
    home: Path
    registry_root: Path
    ledger_path: Path | None = None
    delegation_health_path: Path | None = None
    now: str
    correlation_id: UUID = Field(default_factory=uuid4)

    @field_validator("home", "registry_root")
    @classmethod
    def absolute_paths(cls, path: Path) -> Path:
        if not path.is_absolute():
            raise ValueError("tool collection paths must be absolute")
        return path

    @field_validator("ledger_path", "delegation_health_path")
    @classmethod
    def optional_absolute_paths(cls, path: Path | None) -> Path | None:
        if path is not None and not path.is_absolute():
            raise ValueError("tool collection paths must be absolute")
        return path

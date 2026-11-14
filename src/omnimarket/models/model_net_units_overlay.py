# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host net units overlay document, shared by the net unit installer and the GPU power caps node."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelNetUnitFile(BaseModel):
    """A single file entry in a net unit declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = ""
    content: str = ""
    destination: str
    mode: str
    sha256: str = ""
    sha256_by_host: dict[str, str] = Field(default_factory=dict)
    render: str = ""
    values: dict[str, str] = Field(default_factory=dict)
    encoding: str = ""

    @model_validator(mode="after")
    def _check_source_or_content(self) -> ModelNetUnitFile:
        if bool(self.source) == bool(self.content):
            raise ValueError("a file declares exactly one of source and content")
        return self


class ModelNetUnitDeclaration(BaseModel):
    """A single unit declaration in the overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    platform: str
    hosts: list[str]
    service: str = ""
    # A `systemd-system-slice` unit names the slice it limits; the name must be the basename of one file destination.
    slice: str = ""
    serving: bool = False
    # The budget's numbers are checked by the handler, which refuses each defect with its own typed reason
    # (gpu-budget-incomplete, gpu-budget-no-room, gpu-budget-kv-floor); this document model only requires a mapping.
    gpu_budget: dict[str, float | int] | None = None
    files: list[ModelNetUnitFile]

    @model_validator(mode="after")
    def _check_serving_has_files(self) -> ModelNetUnitDeclaration:
        if self.serving and not self.files:
            raise ValueError("serving unit must declare at least one file")
        return self


class ModelNetUnitsOverlay(BaseModel):
    """The full overlay document for host net units."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    document_schema: Literal["host_net_units.v1"] = Field(alias="schema")
    units: dict[str, ModelNetUnitDeclaration]

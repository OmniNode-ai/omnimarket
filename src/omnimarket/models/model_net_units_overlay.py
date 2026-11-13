# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host net units overlay document, shared by the net unit installer and the GPU power caps node."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)


class ModelNetUnitSecretRef(BaseModel):
    """A secret in the store: the folder and key a ``__SECRET_<NAME>__`` placeholder is filled from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    folder: str = Field(pattern=r"^/")
    key: str = Field(min_length=1)


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
    secrets: dict[str, ModelNetUnitSecretRef] = Field(default_factory=dict)

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
    active_services: list[str] = Field(default_factory=list)
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
    # Where a file's `secrets` are read from: the store addressing and the identity's references, never a value. A document
    # that declares no secrets declares no store.
    secret_store: ModelSecretStoreOverlay | None = None
    units: dict[str, ModelNetUnitDeclaration]

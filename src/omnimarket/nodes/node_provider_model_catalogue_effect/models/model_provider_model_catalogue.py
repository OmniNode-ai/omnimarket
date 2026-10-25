# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed provider-catalogue check request and result."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelProviderModelCatalogueRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    catalogue_json: Path | None = None


class ModelProviderModelCatalogueResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    missing: dict[str, str]

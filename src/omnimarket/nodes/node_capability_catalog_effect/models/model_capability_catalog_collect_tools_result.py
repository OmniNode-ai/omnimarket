# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Terminal payload for a host-local tool collection"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .model_capability_catalog import Catalog


class ModelCapabilityCatalogCollectToolsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    catalog: Catalog

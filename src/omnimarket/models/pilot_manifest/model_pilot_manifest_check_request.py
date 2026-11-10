# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check command: a manifest, and optionally the results read against its bar."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.pilot_manifest.model_pilot_manifest import (
    ModelPilotManifest,
)


class ModelPilotManifestCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    manifest: ModelPilotManifest
    results: dict[str, float] | None = None
    extension_requested: bool = False

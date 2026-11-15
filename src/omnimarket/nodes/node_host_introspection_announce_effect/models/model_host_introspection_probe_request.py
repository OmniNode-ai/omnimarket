# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the announcer asks its gathering effect for on each beat (OMN-19998)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_host_introspection_announcement import (
    EnumHostAnnouncementReason,
)


class ModelHostIntrospectionProbeRequest(BaseModel):
    """Read this host now and assemble its announcement for ``reason``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str = Field(..., min_length=1)
    reason: EnumHostAnnouncementReason


__all__ = ["ModelHostIntrospectionProbeRequest"]

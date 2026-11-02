# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay delivery request and result (OMN-21899)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

ENV_VAR_NAME = "PRINCIPAL_ISSUER_OVERLAY_FILE"


class ModelOverlayDeliveryRequest(BaseModel):
    """What to deliver and where; the host and paths are declared once by the caller's overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    overlay_content: str
    overlay_remote_path: str
    env_file_path: str


class ModelOverlayDeliveryResult(BaseModel):
    """Delivery verdict from what the host reads back, never from what was sent."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    delivered: bool
    reason: str = ""
    overlay_sha256: str = ""
    env_readback_value: str = ""

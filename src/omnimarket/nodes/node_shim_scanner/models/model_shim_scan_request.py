# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for node_shim_scanner."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelShimScanRequest"]


class ModelShimScanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    paths: list[str]
    reference_date: str | None = None
    warn_days_before_expiry: int = Field(default=30, ge=1)

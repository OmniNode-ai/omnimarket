# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Request model for node_shim_scanner."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelShimScanRequest"]


class ModelShimScanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # When None the handler resolves workspace repo roots from the OMNI_HOME
    # environment variable so that ``onex skill shim_audit`` (no-arg) works
    # out of the box.  Pass an explicit list to scope the scan.
    paths: list[str] | None = None
    reference_date: str | None = None
    warn_days_before_expiry: int = Field(default=30, ge=1)

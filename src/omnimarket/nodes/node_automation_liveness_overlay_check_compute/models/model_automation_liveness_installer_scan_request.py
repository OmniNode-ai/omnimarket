# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The files an installer scan reads (OMN-20804)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.automation_liveness_overlay_check import (
    ModelAutomationLivenessScannedFile,
)


class ModelAutomationLivenessInstallerScanRequest(BaseModel):
    """Every candidate file of every scanned repository."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    files: tuple[ModelAutomationLivenessScannedFile, ...]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What an installer scan found (OMN-20804)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .model_automation_liveness_found_source import ModelAutomationLivenessFoundSource


class ModelAutomationLivenessInstallerScanResult(BaseModel):
    """The sources found, sorted by repository and path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sources: tuple[ModelAutomationLivenessFoundSource, ...]

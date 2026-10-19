# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The completeness verdict (OMN-20804)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .model_automation_liveness_finding import ModelAutomationLivenessFinding


class ModelAutomationLivenessOverlayCheckResult(BaseModel):
    """Findings and the counts they were drawn from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[ModelAutomationLivenessFinding, ...]
    entries_checked: int
    sources_found: int
    repositories_scanned: tuple[str, ...]
    complete: bool

    @property
    def passed(self) -> bool:
        return not self.findings

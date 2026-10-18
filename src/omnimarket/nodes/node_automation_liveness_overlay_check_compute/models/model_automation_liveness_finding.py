# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One disagreement between the overlay and the installers (OMN-20804)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .enum_automation_liveness_finding_kind import EnumAutomationLivenessFindingKind


class ModelAutomationLivenessFinding(BaseModel):
    """What is wrong, about which subject, and what to do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumAutomationLivenessFindingKind
    #: The process id, source path or repository the finding is about.
    subject: str
    detail: str

    def render(self) -> str:
        return f"{self.kind.value}: {self.subject}: {self.detail}"

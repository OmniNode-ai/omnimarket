# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Everything the completeness check reads (OMN-20804)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .model_automation_liveness_scanned_file import ModelAutomationLivenessScannedFile


class ModelAutomationLivenessOverlayCheckRequest(BaseModel):
    """The overlay text, the sources declaration text and the scanned files."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_text: str
    sources_text: str
    #: Repositories whose files are in ``files``; a repository with no files still appears here.
    scanned_repositories: tuple[str, ...]
    files: tuple[ModelAutomationLivenessScannedFile, ...]
    #: True when the caller scanned every repository the declaration names. A False run checks
    #: only the repositories it scanned and says so in its result; it never reads as complete.
    complete: bool

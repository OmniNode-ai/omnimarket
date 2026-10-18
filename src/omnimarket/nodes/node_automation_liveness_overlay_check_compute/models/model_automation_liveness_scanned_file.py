# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One text file handed to the installer scan (OMN-20804)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelAutomationLivenessScannedFile(BaseModel):
    """A file's repository, its path inside that repository and its text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str
    path: str
    text: str

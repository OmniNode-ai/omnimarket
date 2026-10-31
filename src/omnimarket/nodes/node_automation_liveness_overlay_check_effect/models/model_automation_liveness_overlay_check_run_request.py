# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one run of the overlay completeness check is asked to read (OMN-20804)."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class ModelAutomationLivenessOverlayCheckRunRequest(BaseModel):
    """The private checkout, where the other clones are, and which files to check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The private checkout holding the overlay and the sources declaration.
    internal_root: Path
    #: The directory holding the other registry clones; None when the caller resolved none.
    registry_root: Path | None = None
    #: The name the sources declaration gives the private checkout (default: its directory name).
    private_repository: str | None = None
    #: Scan only the private checkout; the result says it is partial.
    internal_only: bool = False
    #: The overlay file to check (default: the private checkout's overlay).
    overlay: Path | None = None
    #: The sources declaration to check (default: the private checkout's declaration).
    sources: Path | None = None

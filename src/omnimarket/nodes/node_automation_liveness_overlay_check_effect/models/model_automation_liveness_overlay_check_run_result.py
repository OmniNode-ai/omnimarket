# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one run of the overlay completeness check found, as the command line prints it (OMN-20804)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelAutomationLivenessOverlayCheckRunResult(BaseModel):
    """The exit code and the lines for standard output and standard error.

    Exit 0 means the overlay and the installers agree, 1 means findings, 2 means the check could
    not run (an input is missing, or a repository is absent and ``internal_only`` was not asked for).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout_lines: tuple[str, ...] = ()
    stderr_lines: tuple[str, ...] = ()

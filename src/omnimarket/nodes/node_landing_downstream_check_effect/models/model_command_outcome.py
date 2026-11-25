# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How one subprocess of the downstream check ended."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelCommandOutcome(BaseModel):
    """The exit code and output of one command, whether it timed out, and the OS error class if it never started."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    os_error: str = ""

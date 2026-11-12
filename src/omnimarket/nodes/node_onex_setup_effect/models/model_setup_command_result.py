# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Redacted process evidence returned to setup adapters."""

from pydantic import BaseModel, ConfigDict


class ModelSetupCommandResult(BaseModel):
    """The outcome, including a typed timeout rather than a raw exception."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    returncode: int
    stdout: str
    stderr: str
    argv: tuple[str, ...]
    timed_out: bool = False

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for the pool read share node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelPoolReadShareRequest(BaseModel):
    """One probe a placement needs answered: its argv, the bound it runs under, the share directory and window (seconds)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    directory: str
    argv: tuple[str, ...]
    timeout_s: float
    ttl_s: float


class ModelPoolReadShareResult(BaseModel):
    """The probe's answer. ``shared``: it came from another placement's capture inside the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int
    stdout: str
    stderr: str
    shared: bool

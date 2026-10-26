# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for a lane brief's host prerequisites."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelBriefPrerequisitesRequest:
    brief: str
    pinned_host: str | None


@dataclass(frozen=True)
class ModelBriefPrerequisitesResult:
    host: str | None
    inputs: tuple[str, ...]
    # The brief itself requires ``host`` (a Required host line or host-local inputs), so placement keeps
    # it; False when only the caller's pin names it, a preference a refusing host falls back from (OMN-17427).
    required: bool = False

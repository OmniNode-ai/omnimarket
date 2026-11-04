# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outcome of one context-pack build."""

from __future__ import annotations

from enum import StrEnum


class EnumContextPackOrchestratorStatus(StrEnum):
    """``not_enabled`` carries no package. ``failed`` always carries a failure class; a truncated package is never ``ok``."""

    OK = "ok"
    FAILED = "failed"
    #: An additive call for a profile the server-side list does not enable (OMN-22016).
    NOT_ENABLED = "not_enabled"


__all__ = ["EnumContextPackOrchestratorStatus"]

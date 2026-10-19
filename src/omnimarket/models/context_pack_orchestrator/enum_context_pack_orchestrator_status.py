# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outcome of one context-pack build."""

from __future__ import annotations

from enum import StrEnum


class EnumContextPackOrchestratorStatus(StrEnum):
    """``failed`` always carries a failure class; a truncated package is never ``ok``."""

    OK = "ok"
    FAILED = "failed"


__all__ = ["EnumContextPackOrchestratorStatus"]

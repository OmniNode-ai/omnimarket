# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Outcome status of a task-kind rule evaluation."""

from __future__ import annotations

from enum import StrEnum


class EnumContextTaskKindRulesStatus(StrEnum):
    """``ok`` carries a task kind (``unknown`` when nothing matched); ``error`` carries none."""

    OK = "ok"
    ERROR = "error"


__all__ = ["EnumContextTaskKindRulesStatus"]

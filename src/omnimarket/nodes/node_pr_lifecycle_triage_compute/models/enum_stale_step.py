# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The next stale-summary action for one head."""

from __future__ import annotations

from enum import StrEnum


class EnumStaleStep(StrEnum):
    RERUN = "rerun"
    UPDATE_BRANCH = "update_branch"


__all__: list[str] = ["EnumStaleStep"]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether an evaluated arm is a local rung or a Claude model."""

from __future__ import annotations

from enum import StrEnum


class EnumComparisonArmKind(StrEnum):
    """Closed set."""

    LOCAL = "local"
    CLAUDE = "claude"

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the requesting lane read the PR's lab proof."""

from __future__ import annotations

from enum import StrEnum


class EnumPrHandoffLabProofSource(StrEnum):
    """Where the requesting lane read the PR's lab proof."""

    BODY = "body"
    """A lab line in the PR body."""

    COMMENT = "comment"
    """A PR author's comment with a Lab line naming head=<sha>."""


__all__: list[str] = ["EnumPrHandoffLabProofSource"]

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

    EXEMPT_VERSION_BUMP = "exempt_version_bump"
    """A bot's version-only bump, exempt by operator ruling 2026-10-04T10:11:35Z."""


__all__: list[str] = ["EnumPrHandoffLabProofSource"]

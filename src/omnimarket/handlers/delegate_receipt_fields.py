# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed reads of a delegate receipt's fields (OMN-24173)."""

from __future__ import annotations

from collections.abc import Mapping


def tokens(value: object) -> int:
    """A token count from a receipt value, zero when it is not a number."""
    if not isinstance(value, (str, int, float)):
        return 0
    try:
        return int(value)
    except (ValueError, OverflowError):
        return 0


def model_revision(*blocks: Mapping[str, object]) -> str:
    """The first non-blank model_revision string in the given receipt blocks, or empty."""
    return next(
        (
            found
            for block in blocks
            if isinstance(found := block.get("model_revision"), str) and found.strip()
        ),
        "",
    )

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Splits a Linear ticket id such as ``OMN-23424`` into its prefix and its number."""

from __future__ import annotations

import re

# One ticket id: an upper-case 2-to-5 letter prefix, a hyphen, and the number.
_TICKET_ID = re.compile(r"^([A-Z]{2,5})-([0-9]+)$")


def parse_ticket_id(value: str) -> tuple[str, int]:
    """The (prefix, number) of a Linear ticket id such as ``OMN-23424``."""
    match = _TICKET_ID.match(value.strip())
    if match is None:
        raise ValueError(f"not a ticket id: {value!r}")
    return match.group(1), int(match.group(2))

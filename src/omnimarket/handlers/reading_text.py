# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Text forms a reading's ledger row and token cells share (OMN-22897).

The stamp form and the cell-safe reason token belong to no single node: the reading compute node and
the nodes that keep a reading in the ledger all write the same forms, so they live here rather than in
one node's handler package.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

_TOKEN_LIMIT = 120
_NOT_TOKEN = re.compile(r"[^a-z0-9]+")


def stamp_text(moment: datetime) -> str:
    """The ledger's stamp form, UTC to the second."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def reason_token(text: str) -> str:
    """A free-text reason as one cell-safe token."""
    return _NOT_TOKEN.sub("-", text.lower()).strip("-")[:_TOKEN_LIMIT] or "unspecified"


__all__ = ["reason_token", "stamp_text"]

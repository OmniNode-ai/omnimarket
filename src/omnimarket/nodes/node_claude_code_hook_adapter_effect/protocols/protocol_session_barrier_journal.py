# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The session barrier journal the adapter writes before publishing (OMN-21099)."""

from __future__ import annotations

from typing import Protocol


class ProtocolSessionBarrierJournal(Protocol):
    """Journal each active stream's high-water and return their shared barrier id."""

    def journal(self, session_id: str) -> str: ...

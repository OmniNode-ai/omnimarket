# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The journal an additive call writes its miss record to (OMN-22016, TC.11)."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.claude_code_binding import ModelAdditiveMissRecord


class ProtocolAdditiveMissJournal(Protocol):
    """Journal one miss record; the correlation id joins it to the hook's invocation."""

    def journal(self, record: ModelAdditiveMissRecord, correlation_id: str) -> None: ...

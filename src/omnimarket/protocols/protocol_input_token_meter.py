# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The port that counts a request's input in the served model's own tokens (OMN-26640)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol


class ProtocolInputTokenMeter(Protocol):
    """Counts the tokens a served model will meter for a chat request's messages."""

    def count(
        self,
        *,
        endpoint_ref: str,
        model_id: str,
        messages: Sequence[Mapping[str, str]],
        timeout_s: float,
    ) -> int:
        """The input tokens of ``messages`` on ``endpoint_ref``'s server.

        Raises the meter's own error when the server cannot be asked or answers no count.
        """
        ...


__all__ = ["ProtocolInputTokenMeter"]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the operator reply check (OMN-20894, OMN-20893)."""

from __future__ import annotations

from dataclasses import dataclass

from .enum_access_mode import EnumAccessMode


@dataclass(frozen=True, slots=True)
class ModelOperatorReplyRequest:
    """A message the operator will read.

    The bare-id rule applies in every mode. The local-path rule applies when ``mode`` is REMOTE
    or ``to_phone`` is True (a push always lands on the phone).

    ``recent_replies`` are up to three replies the session sent before this one with no tool call
    between them and this one (the end-of-turn hook reads them from the transcript); a reply that
    repeats one of them is refused.
    """

    text: str
    mode: EnumAccessMode
    to_phone: bool = False
    recent_replies: tuple[str, ...] = ()

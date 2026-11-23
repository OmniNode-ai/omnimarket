# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The verdict on the ask, the line to paste into the draft and what was found."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.cross_owner_ask.enum_cross_owner_ask_verdict import (
    EnumCrossOwnerAskVerdict,
)


class ModelCrossOwnerAskResult(BaseModel):
    """``verified_by`` is the ``verified by: <command> => <observation>`` line the draft carries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: EnumCrossOwnerAskVerdict
    draft_allowed: bool
    verified_by: str
    matches: tuple[str, ...] = ()

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Whether a judged item joined to a run receipt on the caller's machine."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceReceiptJoin(BaseModel):
    """Whether a judged item joined to a run receipt on the caller's machine.

    ``receipt`` is the kind the caller found: ``onex-run``, ``harness`` or ``none``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    receipt: str = Field(pattern="^(onex-run|harness|none)$")

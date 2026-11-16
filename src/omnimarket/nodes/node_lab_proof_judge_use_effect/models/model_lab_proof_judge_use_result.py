# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The result of recording one lab proof judge use."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelLabProofJudgeUseResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_published: bool
    ledger: Literal["written", "already-written", "append-failed", "not-requested"]
    row: str
    event_id: str


__all__ = ["ModelLabProofJudgeUseResult"]

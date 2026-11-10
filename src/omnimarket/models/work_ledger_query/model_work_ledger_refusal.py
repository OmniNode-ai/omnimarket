# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A typed refusal: the request is well formed and its answer would not fit.

The node answers it as a normal terminal answer (``status="refused"``), never as
a raised error, so the runtime does not dead-letter the command and the replay
consumer has nothing to redeliver: the same request refuses the same way on
every attempt, and ``retryable`` says so.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkLedgerRefusal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: Literal["answer_too_large"] = "answer_too_large"
    retryable: Literal[False] = False
    bound_bytes: int = Field(gt=0)
    answer_bytes: int = Field(gt=0)
    matched: int = Field(ge=0)
    suggested_limit: int | None = Field(default=None, ge=1)
    suggestion: str = Field(min_length=1)


__all__ = ["ModelWorkLedgerRefusal"]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One finished delegation run as the AC3 readback measures it (OMN-21326)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelBudgetReading(BaseModel):
    """What a run said about its budget and how long the handler took.

    ``None`` means the run did not say; the readback leaves such a run out of
    the measurement and never reads it as zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    status: str | None
    requested_timeout_seconds: int | None
    execution_timeout_seconds: int | None
    execution_duration_ms: int | None


__all__ = ["ModelBudgetReading"]

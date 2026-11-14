# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The evidence of one pass of the stability lane refresh serve tick."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelStabilityServeEvidence(BaseModel):
    """What one serve pass read, answered and published, and how it ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    records: int
    answered: int
    unreadable: int
    completed: int
    refused: int
    failed: int
    topic: str
    exit_code: int
    correlation_ids: tuple[str, ...]


__all__ = ["ModelStabilityServeEvidence"]

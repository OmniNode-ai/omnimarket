# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transcript source revision ordered by record count, then byte length."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelSourceRevision(BaseModel):
    """Size of a transcript read, independent of its timestamp metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_count: int = Field(ge=0)
    byte_length: int = Field(ge=0)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ModelSourceRevision):
            return NotImplemented
        return (self.record_count, self.byte_length) < (
            other.record_count,
            other.byte_length,
        )

    def __le__(self, other: object) -> bool:
        if not isinstance(other, ModelSourceRevision):
            return NotImplemented
        return (self.record_count, self.byte_length) <= (
            other.record_count,
            other.byte_length,
        )

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, ModelSourceRevision):
            return NotImplemented
        return (self.record_count, self.byte_length) > (
            other.record_count,
            other.byte_length,
        )

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, ModelSourceRevision):
            return NotImplemented
        return (self.record_count, self.byte_length) >= (
            other.record_count,
            other.byte_length,
        )

    def satisfies(self, required: ModelSourceRevision) -> bool:
        """Whether this read is at least as complete as the required read."""
        return self >= required

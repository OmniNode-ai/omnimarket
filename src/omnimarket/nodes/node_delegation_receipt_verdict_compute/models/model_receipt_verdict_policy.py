# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract topics and deployment-owned task allowlist."""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelReceiptVerdictPolicy(BaseModel):
    """Receipt topic identities are contract data, never inferred from payloads."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    admission_topic: str = Field(min_length=1)
    completed_topic: str = Field(min_length=1)
    failed_topic: str = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_topics(self) -> Self:
        if len({self.admission_topic, self.completed_topic, self.failed_topic}) != 3:
            raise ValueError("receipt topics must be distinct")
        return self


class ModelReceiptVerdictOverlay(BaseModel):
    """The installed client's explicitly permitted task types; empty means none."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    allowlisted_task_types: tuple[str, ...]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only consumer evidence collected after an SCM terminal timeout."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelScmConsumerEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: Literal[
        "no_consumer_for_topic",
        "consumer_present_no_terminal",
        "consumer_evidence_unreadable",
    ]
    command_topic: str
    physical_topic: str | None = None
    consumer_groups: tuple[str, ...] = ()
    consumer_count: int | None = Field(default=None, ge=0)
    detail: str = ""

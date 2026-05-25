# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelTicketResearchEnrichmentResult — output from the research enrichment compute node."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class EnumEnrichmentStatus(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    ERROR = "error"
    SKIPPED = "skipped"


class ModelTicketResearchEnrichmentResult(BaseModel):
    """Result of a knowledge context enrichment request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumEnrichmentStatus = Field(
        ..., description="Outcome of the enrichment attempt"
    )
    context_path: Path | None = Field(
        default=None,
        description="Absolute path to the written context file (None when skipped/error/timeout)",
    )
    error_message: str | None = Field(
        default=None,
        description="Error detail when status is error or timeout",
    )


__all__: list[str] = ["EnumEnrichmentStatus", "ModelTicketResearchEnrichmentResult"]

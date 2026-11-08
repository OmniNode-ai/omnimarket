# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refusal records of one window, as the hand-off wrote them."""

from pydantic import BaseModel, ConfigDict, Field


class ModelRefusalRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(
        description="Where the record was read: a detail file or ledger line, as the caller names it."
    )
    text: str = Field(
        description="The refusal's own text, e.g. a 'handoff_row: <repo>#<n> NOT-READY ...' line."
    )


class ModelInstallRefusalTraceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: str = Field(
        description="UTC start of the window, as the caller states it."
    )
    window_end: str = Field(
        description="UTC end of the window, as the caller states it."
    )
    records: tuple[ModelRefusalRecord, ...] = Field(
        description="Every refusal record read for the window."
    )

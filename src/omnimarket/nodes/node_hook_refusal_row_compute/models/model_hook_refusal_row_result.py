# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The decided refusal: normalised fields, dedupe key and the ledger row."""

from pydantic import BaseModel, ConfigDict, Field


class ModelHookRefusalRowResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    guard: str = Field(description="Redacted guard id, at most 64 characters.")
    reason: str = Field(description="Low-cardinality dedupe token for the reason.")
    detail: str = Field(description="Redacted detail, at most 240 characters.")
    key: str = Field(description="Twelve-character dedupe key of the refusal class.")
    repeated_secret: bool = Field(
        description="True when the secret guard's per-session retry budget applies."
    )
    row: str = Field(description="One pipe-delimited FRICTION ledger row.")

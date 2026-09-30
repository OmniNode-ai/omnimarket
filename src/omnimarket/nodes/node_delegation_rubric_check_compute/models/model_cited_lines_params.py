# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contract parameters for cited_lines."""

from pydantic import BaseModel, ConfigDict, Field


class ModelCitedLinesParams(BaseModel):
    """Validated, caller-supplied criterion configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_tolerance: int = Field(ge=0)
    min_quote_chars: int = Field(ge=1)
    no_findings_sentinels: tuple[str, ...]

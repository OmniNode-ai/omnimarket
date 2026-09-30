# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contract parameters for id_coverage."""

from pydantic import BaseModel, ConfigDict, Field


class ModelIdCoverageParams(BaseModel):
    """Validated, caller-supplied criterion configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id_pattern: str = Field(min_length=1)
    trigger_patterns: tuple[str, ...]

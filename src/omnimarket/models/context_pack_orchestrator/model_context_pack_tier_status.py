# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One tier's outcome: what its source answered, and whether the package carries it."""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_error_code import (
    EnumContextSourceErrorCode,
)
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from pydantic import BaseModel, ConfigDict, Field


class ModelContextPackTierStatus(BaseModel):
    """A tier that is not ``resolved`` is omitted from the package and listed here."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: EnumContextFactor
    required: bool
    status: EnumContextSourceStatus
    error_code: EnumContextSourceErrorCode | None = None
    error_message: str | None = None
    entry_count: int = Field(default=0, ge=0)
    token_estimate: int = Field(default=0, ge=0)
    included: bool = False
    #: Entries the source answered that selection inside the tier left out (OMN-21111).
    trimmed_entry_count: int = Field(default=0, ge=0)


__all__ = ["ModelContextPackTierStatus"]

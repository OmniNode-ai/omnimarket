# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Summary result model for a single KreuzbergParseEffect run."""

from pydantic import BaseModel, ConfigDict, Field


class ModelKreuzbergParseResult(BaseModel):
    """Summary of documents processed in a single handler invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    indexed_count: int = Field(..., ge=0)
    failed_count: int = Field(..., ge=0)
    skipped_too_large_count: int = Field(..., ge=0)
    timeout_count: int = Field(..., ge=0)

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelIngestionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_paths: list[str]
    content_type: str | None = None
    correlation_id: str | None = None
    exclude_patterns: list[str] | None = None

# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelIngestionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    root_paths: list[str]
    exclude_patterns: list[str] | None = None

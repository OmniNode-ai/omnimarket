# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelExtractionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_path: str
    content_type: str | None = None
    extracted_text: str | None = None
    extraction_error: str | None = None

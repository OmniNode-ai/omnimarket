# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ModelDocumentEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_path: str
    repo_name: str
    git_sha: str | None
    author: str | None
    created_at: datetime | None
    updated_at: datetime | None
    file_size_bytes: int
    source_content_sha256: str


class ModelIngestionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    documents: list[ModelDocumentEntry]

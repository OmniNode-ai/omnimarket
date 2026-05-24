# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# [OMN-11914]

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelKBRepoIndexRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kb_repo: str = Field(
        default="OmniNode-ai/knowledge-base",
        description="GitHub repo slug for the knowledge-base repo",
    )
    dry_run: bool = Field(
        default=False,
        description="Preview index invocation without writing state",
    )

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelSubtask(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subtask_id: str
    description: str
    model_affinity: str = ""
    depends_on: tuple[str, ...] = ()
    estimated_tokens: int = 0
    category: str = "general"

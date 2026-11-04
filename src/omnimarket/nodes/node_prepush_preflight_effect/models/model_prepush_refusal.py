# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One pre-push hook's refusal: the hook id and the finding it printed."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelPrepushRefusal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    hook_id: str
    name: str = ""
    exit_code: int | None = None
    finding: str = ""

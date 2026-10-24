# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One standup draft and optional facts to validate."""

from pydantic import BaseModel, ConfigDict


class ModelStandupDraftCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str
    facts: dict[str, object] | None = None

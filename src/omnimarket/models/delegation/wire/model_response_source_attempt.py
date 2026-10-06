# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Identity of the attempt that supplied a delegation terminal's response."""

from pydantic import BaseModel, ConfigDict, Field


class ModelResponseSourceAttempt(BaseModel):
    """Index and routing identity within the terminal's recorded attempts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_index: int = Field(ge=0)
    tier: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)

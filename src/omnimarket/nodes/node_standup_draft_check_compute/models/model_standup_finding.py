# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A named rule violation and its repair instruction."""

from pydantic import BaseModel, ConfigDict


class ModelStandupFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    rule: str
    message: str

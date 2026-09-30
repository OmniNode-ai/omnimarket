# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contract parameters for test_targets."""

from pydantic import BaseModel, ConfigDict, Field


class ModelTestTargetsParams(BaseModel):
    """Validated, caller-supplied criterion configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    target_pattern: str = Field(min_length=1)

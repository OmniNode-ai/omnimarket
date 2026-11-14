# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One contract's topic facts for the provision plan."""

from pydantic import BaseModel, ConfigDict, Field


class ModelTopicProvisionContract(BaseModel):
    """The topics and runtime profiles one contract declares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_name: str = Field(min_length=1)
    runtime_profiles: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()

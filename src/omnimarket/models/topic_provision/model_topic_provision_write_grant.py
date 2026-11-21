# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A broker scope overlay's WRITE grant of one principal on one literal topic."""

from pydantic import BaseModel, ConfigDict, Field


class ModelTopicProvisionWriteGrant(BaseModel):
    """The principal (without the ``User:`` prefix) and the topic it may write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: str = Field(min_length=1)
    topic: str = Field(min_length=1)

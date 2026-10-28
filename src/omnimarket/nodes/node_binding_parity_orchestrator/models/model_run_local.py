# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Configuration of the in-process entry: the topics and the wait, all from the contract's ``config``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRunLocalConfig(BaseModel):
    """``config.run_local`` of the node's contract; every key is required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_topic: str = Field(min_length=1)
    result_topic: str = Field(min_length=1)
    consumer_group: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)

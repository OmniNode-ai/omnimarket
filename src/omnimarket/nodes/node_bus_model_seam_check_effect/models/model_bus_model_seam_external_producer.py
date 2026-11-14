# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A producer model published by an external system."""

from pydantic import BaseModel, ConfigDict


class ModelBusModelSeamExternalProducer(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    topic: str
    model: str

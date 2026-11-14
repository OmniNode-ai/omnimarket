# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A graded producer/consumer bus-model pair."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelBusModelSeamPair(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    topic: str
    producer_node: str
    producer_model: str
    consumer_node: str
    consumer_model: str
    source: Literal["explicit", "inferred"]
    refused: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    unresolved: str = ""

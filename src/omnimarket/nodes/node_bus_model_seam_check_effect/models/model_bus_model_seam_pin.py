# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A pinned bus-model seam debt."""

from pydantic import BaseModel, ConfigDict


class ModelBusModelSeamPin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    topic: str
    producer_model: str
    consumer_model: str
    refused: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus-model seam check policy."""

from pydantic import BaseModel, ConfigDict

from .model_bus_model_seam_external_producer import (
    ModelBusModelSeamExternalProducer,
)
from .model_bus_model_seam_pin import ModelBusModelSeamPin


class ModelBusModelSeamPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    strict_models: tuple[str, ...] = ()
    external_producers: tuple[ModelBusModelSeamExternalProducer, ...] = ()
    pins: tuple[ModelBusModelSeamPin, ...] = ()

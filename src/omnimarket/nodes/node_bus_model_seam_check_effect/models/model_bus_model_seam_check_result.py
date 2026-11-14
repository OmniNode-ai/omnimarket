# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed bus-model seam check result."""

from pydantic import BaseModel, ConfigDict

from .model_bus_model_seam_pair import ModelBusModelSeamPair
from .model_bus_model_seam_pin import ModelBusModelSeamPin


class ModelBusModelSeamCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    admitted: bool
    exit_code: int
    output_lines: tuple[str, ...]
    pair_count: int
    explicit_pair_count: int
    inferred_pair_count: int
    clean_pair_count: int
    pinned_pair_count: int
    unpinned_pairs: tuple[ModelBusModelSeamPair, ...] = ()
    stale_pins: tuple[ModelBusModelSeamPin, ...] = ()
    strict_failures: tuple[str, ...] = ()
    pairs: tuple[ModelBusModelSeamPair, ...] = ()

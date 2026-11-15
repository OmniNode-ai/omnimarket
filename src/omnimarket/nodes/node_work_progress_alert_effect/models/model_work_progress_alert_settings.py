# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The effect's gathering settings; supplied by the overlay, never packaged."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus


class ModelIgnoreList(BaseModel):
    """Lanes the alert never judges: lane-id globs and ``actor=`` globs."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    lanes: list[str] = Field(description="fnmatch globs matched against the lane id")
    actors: list[str] = Field(
        description="fnmatch globs matched against any row's actor= cell"
    )


class ModelWorkProgressAlertSettings(BaseModel):
    """Required overlay document; every field is a deployment decision."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    ignore: ModelIgnoreList
    lookback_seconds: PositiveInt
    check_interval_seconds: PositiveInt
    event_bus: ModelNodeEventBus | None = Field(default=None, strict=False)

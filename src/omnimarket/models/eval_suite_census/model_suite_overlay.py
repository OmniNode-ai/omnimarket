# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_code_eval_suite_census_compute (OMN-23883)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus
from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)


class ModelCodeEvalSuiteCensusOverlay(BaseModel):
    """The overlay document: one key holding the capability line.

    Every sample size the census answers with comes from this line; the
    contract holds none of the values. The optional ``event_bus`` key is read
    by the bus overlay reader behind ``onex run-node`` and the node never reads
    it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: ModelCapabilityLine
    event_bus: ModelNodeEventBus | None = None


__all__ = ["ModelCodeEvalSuiteCensusOverlay"]

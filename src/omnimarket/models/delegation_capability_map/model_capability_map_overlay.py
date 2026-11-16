# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment overlay document for the capability map's class line."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)


class ModelCapabilityMapOverlay(BaseModel):
    """The overlay document of node ``node_projection_delegation_capability_map``.

    Its single key is ``delegation.capability_map.line``: the class line a
    capability cell is judged against. There is no packaged value; the values
    come from the deployment overlay.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: ModelCapabilityLine

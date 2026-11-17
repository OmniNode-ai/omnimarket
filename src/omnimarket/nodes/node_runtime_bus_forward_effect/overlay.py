# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_runtime_bus_forward_effect: every forward setting, none in code.

Found at ``<root>/node_runtime_bus_forward_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_runtime_bus_forward_effect"


class ModelHostedEventBus(BaseModel):
    """The hosted bus a run's recorded events are carried to, as this node's own overlay declares it.

    ``lane`` names the lane declaration entry the adapter resolves to its broker and client transport (the way
    ``onex delegate`` and ``onex scm`` resolve a lane); the broker address is never in this overlay and never in code.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["kafka"]
    lane: str = Field(min_length=1)


class ModelRuntimeBusForwardOverlay(BaseModel):
    """Every setting of the forward, as the deployment's overlay supplies it.

    ``max_events`` caps how many recorded events one run carries (an event past the cap is refused, never dropped
    silently); ``flush_timeout_seconds`` bounds the wait for delivery confirmation; ``allowed_topic_prefixes`` is the
    whole allow-list — an event whose topic is under none of the prefixes is refused before any publish.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_bus: ModelHostedEventBus
    max_events: int = Field(ge=1)
    flush_timeout_seconds: float = Field(gt=0)
    allowed_topic_prefixes: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelRuntimeBusForwardOverlay:
        for prefix in self.allowed_topic_prefixes:
            if not prefix.strip():
                raise ValueError("an allowed topic prefix is empty")
            if not prefix.endswith("."):
                raise ValueError("an allowed topic prefix ends with a dot")
        return self


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelRuntimeBusForwardOverlay:
    return load_structured_overlay(NODE_NAME, ModelRuntimeBusForwardOverlay, environ)

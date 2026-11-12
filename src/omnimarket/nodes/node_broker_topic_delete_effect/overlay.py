# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_broker_topic_delete_effect: every setting of the delete, none in code.

Found at ``<root>/node_broker_topic_delete_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_broker_topic_removal_effect.overlay import (
    ModelContractSource,
)

NODE_NAME = "node_broker_topic_delete_effect"


class ModelTopicDeleteOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contracts: ModelContractSource
    #: Topic name prefixes the delete never touches.
    protected_prefixes: tuple[str, ...]
    #: A topic whose newest retained record is younger than this is still produced to.
    recent_producer_days: int = Field(ge=1)
    #: The most topics one command may name.
    max_topics_per_command: int = Field(ge=1)
    #: Whether a command may issue deletes at all; a report-only run only names what would go.
    enforce: bool
    #: The declared bus lane whose machine credential the broker admin client uses.
    bus_lane: str = Field(min_length=1)
    #: Client id the admin client presents while reading live evidence.
    probe_client_id: str = Field(min_length=1)
    request_timeout_seconds: float = Field(gt=0)


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelTopicDeleteOverlay:
    return load_structured_overlay(NODE_NAME, ModelTopicDeleteOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelTopicDeleteOverlay",
    "load_overlay",
]

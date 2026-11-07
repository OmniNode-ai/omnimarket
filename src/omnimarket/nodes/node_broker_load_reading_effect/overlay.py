# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_broker_load_reading_effect: every setting of the broker load reading, none in code.

Found at ``<root>/node_broker_load_reading_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_broker_load_reading_effect"


class ModelBrokerMetricsSource(BaseModel):
    """Where the broker's public metrics are read: inside its container, over ssh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_host_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    compose_project: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    service: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    #: The metrics address as the container itself reaches it.
    metrics_url: str = Field(pattern=r"^http://localhost:[0-9]+/[A-Za-z0-9_./-]+$")
    read_timeout_seconds: int = Field(ge=1)


class ModelConsumerProbe(BaseModel):
    """The probe consumer whose start time is the reading's symptom side."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``inmemory`` answers from a fixture; ``kafka`` starts a real consumer on the lane's broker.
    backend: Literal["inmemory", "kafka"]
    #: The bus lane whose broker and stored login the kafka backend resolves.
    lane: str = Field(min_length=1)
    #: An existing topic to subscribe to; none is created.
    probe_topic: str = Field(min_length=1)
    #: The consumer group prefix the broker grants the probing principal.
    group_prefix: str = Field(min_length=1)
    #: The start is given up after this long and reads as a timeout.
    deadline_seconds: float = Field(gt=0)


class ModelBrokerBars(BaseModel):
    """The bars the reading is judged against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_partitions_per_shard: int = Field(ge=1)
    max_consumer_start_ms: int = Field(ge=1)


class ModelBrokerLoadPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published reading stays fresh; older, it reads NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)


class ModelBrokerLoadOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    metrics_source: ModelBrokerMetricsSource
    consumer: ModelConsumerProbe
    bars: ModelBrokerBars
    ledger_append: ModelLedgerAppendRoute
    publish: ModelBrokerLoadPublish
    injected_fault: str = Field(min_length=1)

    def overlay_hash(self) -> str:
        """Hash of what the verdict depends on, so a bar change shows in the row."""
        text = json.dumps(
            {
                "bars": self.bars.model_dump(mode="json"),
                "consumer": self.consumer.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelBrokerLoadOverlay:
    return load_structured_overlay(NODE_NAME, ModelBrokerLoadOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelBrokerBars",
    "ModelBrokerLoadOverlay",
    "ModelBrokerLoadPublish",
    "ModelBrokerMetricsSource",
    "ModelConsumerProbe",
    "load_overlay",
]

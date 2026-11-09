# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_broker_auth_refusal_reading_effect: every setting of the reading, none in code.

Found at ``<root>/node_broker_auth_refusal_reading_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_broker_auth_refusal_reading_effect"


class ModelBrokerLogSource(BaseModel):
    """Where the broker's log is read: its container's log, over ssh."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_host_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    compose_project: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    service: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    read_timeout_seconds: int = Field(ge=1)


class ModelRefusalReading(BaseModel):
    """What counts as a refusal and how long a look back one reading takes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The text the broker's log holds on an authentication refusal.
    needle: str = Field(min_length=1)
    window_seconds: int = Field(ge=60)
    sample_max_chars: int = Field(ge=1)


class ModelRefusalBars(BaseModel):
    """The bar the reading is judged against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Refusals in the window at or under this read WORKING; zero means any refusal is a finding.
    max_refusals: int = Field(ge=0)


class ModelRefusalPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published reading stays fresh; older, it reads NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)


class ModelBrokerAuthRefusalOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    log_source: ModelBrokerLogSource
    reading: ModelRefusalReading
    bars: ModelRefusalBars
    ledger_append: ModelLedgerAppendRoute
    publish: ModelRefusalPublish
    injected_fault: str = Field(min_length=1)

    def overlay_hash(self) -> str:
        """Hash of what the verdict depends on, so a bar or window change shows in the row."""
        text = json.dumps(
            {
                "bars": self.bars.model_dump(mode="json"),
                "reading": self.reading.model_dump(mode="json"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelBrokerAuthRefusalOverlay:
    return load_structured_overlay(NODE_NAME, ModelBrokerAuthRefusalOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelBrokerAuthRefusalOverlay",
    "ModelBrokerLogSource",
    "ModelRefusalBars",
    "ModelRefusalPublish",
    "ModelRefusalReading",
    "load_overlay",
]

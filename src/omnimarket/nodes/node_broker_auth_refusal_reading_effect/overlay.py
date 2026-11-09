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
    #: Message forms by name: a refusal line holding the text is counted under its name (first match wins).
    forms: dict[str, str] = Field(min_length=1)


class ModelControlMarker(BaseModel):
    """The deploy's own unauthenticated negative control, told apart from every other client (OMN-18012).

    The control is ``rpk topic list`` with no credentials, run once per deploy by a one-shot container; the
    broker refuses it by design, which proves an unauthenticated client is refused. Its marker is its source
    container and the span of its run: a refusal inside that span is the control, up to ``max_lines_per_run``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    container: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    #: Slack either side of the container's span, for the clock between the broker's log and the container's.
    tolerance_seconds: float = Field(ge=0)
    #: Refusals one control run explains; every refusal past it inside the span is another client.
    max_lines_per_run: int = Field(ge=1)


class ModelRefusalBars(BaseModel):
    """The bar the reading is judged against."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Refusals that are not the control, in the window, at or under this read WORKING; zero means any is a finding.
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
    control: ModelControlMarker
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
                "control": self.control.model_dump(mode="json"),
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
    "ModelControlMarker",
    "ModelRefusalBars",
    "ModelRefusalPublish",
    "ModelRefusalReading",
    "load_overlay",
]

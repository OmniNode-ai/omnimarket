# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_lab_alarm_deliver_effect: every destination, window and cell of the delivery (OMN-23906).

Found at ``<root>/node_lab_alarm_deliver_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` (the in-memory backend,
synthetic values). Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_lab_alarm_deliver_effect"


class ModelChannelSettings(BaseModel):
    """The alert channel the alarm is posted to, through the alert-channel library's alarm function."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The shell library that defines ``alert_channel_alarm``; it resolves the channel and the credentials itself.
    library_path: str = Field(min_length=1)
    #: The category ``alert_channel_alarm`` is called with.
    category: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    #: ``{condition}``, ``{subject}`` and ``{detail}`` are filled in.
    message_template: str = Field(min_length=1)

    @field_validator("message_template")
    @classmethod
    def _placeholders(cls, value: str) -> str:
        for name in ("{condition}", "{subject}", "{detail}"):
            if name not in value:
                raise ValueError(f"the message template names no {name}")
        return value


class ModelLedgerSettings(BaseModel):
    """The FRICTION row a raised alarm appends to the rolling ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The bus lane the ledger host's append is reached over (the ledger write path, not a file).
    bus_lane: str = Field(min_length=1)
    #: The registry root the bus opener resolves the lane's stored login under.
    workspace_root: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    friction_class: str = Field(min_length=1)
    #: The ticket the friction guard demands as ``existing=``.
    ticket: str = Field(pattern=r"^OMN-\d+$")
    #: The cost cell: a friction row must carry a figure.
    cost: str = Field(min_length=1)
    #: The uuid namespace the per-episode request id is derived in.
    request_namespace: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    #: Longest evidence quoted in the row.
    evidence_limit: int = Field(ge=1)
    #: A subject that clears and re-raises inside this many minutes appends no second row.
    dedupe_window_minutes: int = Field(ge=1)


class ModelDeliveryOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``inmemory`` records instead of posting; ``runtime`` posts to the channel and appends over the bus.
    backend: Literal["inmemory", "runtime"]
    channel: ModelChannelSettings
    ledger: ModelLedgerSettings
    #: Where the episodes (active subjects, last ledger row, rows still to write) are kept between ticks.
    state_path: str = Field(min_length=1)


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelDeliveryOverlay:
    return load_structured_overlay(NODE_NAME, ModelDeliveryOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelChannelSettings",
    "ModelDeliveryOverlay",
    "ModelLedgerSettings",
    "load_overlay",
]

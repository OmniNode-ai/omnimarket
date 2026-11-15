# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The drain command a pool host takes over the bus (OMN-24489).

A host leaves placement by a command, not by a data pull request: the drain
mark rides the bus to the host, which holds it and advertises it and its
running lanes on every beat. The command is validated into
:class:`ModelHostDrainCommand`; the topic it travels on is read from the
contract of ``omnimarket.nodes.node_host_drain_effect``.
"""

from __future__ import annotations

import importlib.resources
from datetime import datetime

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelHostDrainCommand(BaseModel):
    """A drain or undrain mark for one pool host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str = Field(min_length=1)
    draining: bool
    until: datetime | None = None
    reason: str = ""
    issued_at: datetime
    correlation_id: str = ""

    @field_validator("issued_at")
    @classmethod
    def _aware_issued_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("issued_at must be timezone-aware")
        return value

    @field_validator("until")
    @classmethod
    def _aware_until(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("until must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _drain_consistent(self) -> ModelHostDrainCommand:
        if self.draining and self.until is None:
            raise ValueError("a draining command must carry until")
        if not self.draining and self.until is not None:
            raise ValueError("an undrain command must not carry until")
        return self

    def reads_draining(self, now: datetime) -> bool:
        """Whether this mark still reads draining at ``now`` (a mark past its expiry reads as not draining)."""
        if not self.draining or self.until is None:
            return False
        return now < self.until


def load_host_drain_topic() -> str:
    """The topic a drain command travels on, read from the contract of ``omnimarket.nodes.node_host_drain_effect``."""
    contract = (
        importlib.resources.files("omnimarket.nodes.node_host_drain_effect")
        / "contract.yaml"
    )
    text = contract.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    topic = data["topics"]["host_drain_requested"]
    if not isinstance(topic, str) or not topic:
        raise ValueError("the contract names no host_drain_requested topic")
    return topic


__all__ = [
    "ModelHostDrainCommand",
    "load_host_drain_topic",
]

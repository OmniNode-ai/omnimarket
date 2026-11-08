# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_lab_alarm: every setting of the broker lag and effects-held conditions.

Found at ``<root>/node_lab_alarm/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
The package ships ``overlay.example.yaml`` (the in-memory backend, synthetic values). Nothing here has
a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_lab_alarm"


class ModelAlarmBackend(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``inmemory`` reads fixtures; ``runtime`` reads the lane's manifest URL and broker.
    backend: Literal["inmemory", "runtime"]
    #: The bus lane whose broker address and stored login the runtime backend resolves.
    lane: str = Field(min_length=1)


class ModelDeclaredNode(BaseModel):
    """One node whose runtime consumer groups the condition watches."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package: str = Field(min_length=1)
    node: str = Field(min_length=1)


class ModelGroupDerivation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The two segments no contract declares.
    env: str = Field(min_length=1)
    instance: str = Field(min_length=1)
    nodes: tuple[ModelDeclaredNode, ...] = Field(min_length=1)


class ModelManifestSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)


class ModelLagSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: float = Field(gt=0)
    #: Where the previous sample is kept between ticks.
    state_path: str = Field(min_length=1)


class ModelEscalationSettings(BaseModel):
    """The lane an alarm that names no owner starts: the dispatch command and what it is told."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: argv of the lane dispatcher; the brief file, lane name, model and ticket are appended.
    command: tuple[str, ...] = Field(min_length=1)
    model: str = Field(min_length=1)
    ticket: str = Field(pattern=r"^OMN-\d+$")
    #: A dispatcher still running after this many seconds has started its lane.
    settle_seconds: float = Field(gt=0)
    #: Where the briefs and dispatch logs are kept.
    brief_dir: str = Field(min_length=1)


class ModelEffectsHeldSettings(BaseModel):
    """The effects-held condition (OMN-23106): which groups, how long a hold counts, who is told."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The consumer group embeds the node's contract version, so it is matched by prefix and suffix.
    group_prefix: str = Field(min_length=1)
    group_suffix: str = Field(min_length=1)
    #: How long no group may hold a live member before this alarms.
    held_after_seconds: float = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    #: Where the hold clock and the episode's lane are kept between ticks.
    state_path: str = Field(min_length=1)
    escalation: ModelEscalationSettings


class ModelLabAlarmOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bus: ModelAlarmBackend
    manifest: ModelManifestSettings
    groups: ModelGroupDerivation
    lag: ModelLagSettings
    injected_fault: str = Field(min_length=1)
    effects_held: ModelEffectsHeldSettings

    @model_validator(mode="after")
    def _shape(self) -> ModelLabAlarmOverlay:
        keys = [(n.package, n.node) for n in self.groups.nodes]
        if len(keys) != len(set(keys)):
            raise ValueError("a declared node is listed twice")
        return self


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelLabAlarmOverlay:
    return load_structured_overlay(NODE_NAME, ModelLabAlarmOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelAlarmBackend",
    "ModelDeclaredNode",
    "ModelEffectsHeldSettings",
    "ModelEscalationSettings",
    "ModelGroupDerivation",
    "ModelLabAlarmOverlay",
    "ModelLagSettings",
    "ModelManifestSettings",
    "load_overlay",
]

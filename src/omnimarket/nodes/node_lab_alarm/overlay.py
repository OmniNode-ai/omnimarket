# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_lab_alarm: every setting of the broker lag, effects-held and broker-listener conditions.

Found at ``<root>/node_lab_alarm/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
The package ships ``overlay.example.yaml`` (the in-memory backend, synthetic values). Nothing here has
a default: an absent key refuses the load and names it, with one exception: ``effects_held`` is the
block of the effects-held verbs only, so an overlay that lacks it still loads and the broker lag read
(the verb the schedule runs) keeps reporting; the effects-held verbs refuse it themselves (OMN-23340).
``broker_listener`` is the same: the block of the broker-listener verbs only, and those verbs refuse
it themselves.
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


class ModelManifestResilience(BaseModel):
    """How the alarm treats a manifest endpoint that does not answer (it is down while dev redeploys).

    Retries and the last-good fallback never touch the lag rule or any lag bound: the broker is still
    read for every group, the fallback only supplies the group names, and its age is named in the result.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Fetch attempts per run, the first included.
    attempts: int = Field(ge=1)
    retry_delay_seconds: float = Field(ge=0)
    #: The oldest last-good manifest the run may take its group names from; older reads INDETERMINATE.
    max_stale_seconds: int = Field(ge=1)
    #: Where the last good manifest (only the declared nodes' contracts) is kept.
    cache_path: str = Field(min_length=1)


class ModelManifestSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    #: Absent means one attempt and no fallback (the behaviour before OMN-22874); declare it to turn both on.
    resilience: ModelManifestResilience | None = None


class ModelLagGrowthRule(BaseModel):
    """When growth is an alarm: a rebalance after a dev redeploy grows a group for a run or two, a stuck group keeps growing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Messages a group must grow by between two samples for that sample to count as growth.
    min_growth: int = Field(ge=1)
    #: Consecutive samples that count as growth before the group alarms.
    persist_runs: int = Field(ge=1)
    #: Where each group's count of consecutive growth samples is kept between ticks.
    streak_path: str = Field(min_length=1)


class ModelLagSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: float = Field(gt=0)
    #: Where the previous sample is kept between ticks.
    state_path: str = Field(min_length=1)
    #: Where each finished run is appended, in the shape the floor's broker lag source reads.
    record_path: str = Field(min_length=1)
    #: The condition name the run record carries, the one the floor's overlay lists for the broker.
    record_condition: str = Field(min_length=1)
    #: Absent means the rule before OMN-19998: any growth between two samples alarms at once. Declare it to turn the persistence on.
    growth: ModelLagGrowthRule | None = None


class ModelLabAlarmScheduleHost(BaseModel):
    """A host that runs the broker lag read on the schedule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user", "launchd"]
    #: The venv whose interpreter runs the unit, relative to the registry root. A kept tree, never a worktree.
    venv: str = Field(min_length=1)
    #: Environment the host's unit carries; ``$OMNI_HOME`` and ``$HOME`` are filled in at install.
    environment: dict[str, str]


class ModelLabAlarmSchedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    launchd_label: str = Field(min_length=1)
    #: Relative to the registry root: the unit's log directory.
    state_directory: str = Field(min_length=1)
    #: Seconds between two runs. The floor reads the record no older than its own bound, so keep it well under.
    interval_seconds: int = Field(ge=60)
    timeout_seconds: int = Field(ge=1)
    hosts: tuple[ModelLabAlarmScheduleHost, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_hosts(self) -> ModelLabAlarmSchedule:
        names = [host.name for host in self.hosts]
        if len(names) != len(set(names)):
            raise ValueError("a schedule host is declared twice")
        return self

    def host(self, name: str) -> ModelLabAlarmScheduleHost | None:
        return next((host for host in self.hosts if host.name == name), None)


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


class ModelBrokerListenerSettings(BaseModel):
    """The broker-listener condition (OMN-23933): which listener addresses to dial, and for how long."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The dev broker's external listener addresses, the old config key ``kafka_bootstrap_servers``.
    bootstrap_servers: str = Field(min_length=1)
    #: The old script's own 10 s dial bound.
    timeout_seconds: float = Field(gt=0)


class ModelAlarmDeliveryWiring(BaseModel):
    """Names that this node's alarms are delivered through node_lab_alarm_deliver_effect (OMN-23906).

    That node's own overlay holds every destination, window and cell; this block only turns the delivery on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool


class ModelLabAlarmOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    bus: ModelAlarmBackend
    manifest: ModelManifestSettings
    groups: ModelGroupDerivation
    lag: ModelLagSettings
    schedule: ModelLabAlarmSchedule
    injected_fault: str = Field(min_length=1)
    #: Read by the effects-held verbs alone. Absent, the broker lag verbs still run: a block that
    #: belongs to one condition must not stop another condition's reading (the 2026-11-09 stale read).
    effects_held: ModelEffectsHeldSettings | None = None
    #: Read by the broker-listener verbs alone; absent, the overlay still loads and the other verbs
    #: run, and the broker-listener verbs refuse it themselves, as the effects-held ones do theirs.
    broker_listener: ModelBrokerListenerSettings | None = None
    #: Absent, nothing is delivered: an overlay written before the delivery existed loads and runs unchanged.
    alarm_delivery: ModelAlarmDeliveryWiring | None = None

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
    "ModelAlarmDeliveryWiring",
    "ModelBrokerListenerSettings",
    "ModelDeclaredNode",
    "ModelEffectsHeldSettings",
    "ModelEscalationSettings",
    "ModelGroupDerivation",
    "ModelLabAlarmOverlay",
    "ModelLabAlarmSchedule",
    "ModelLabAlarmScheduleHost",
    "ModelLagGrowthRule",
    "ModelLagSettings",
    "ModelManifestResilience",
    "ModelManifestSettings",
    "load_overlay",
]

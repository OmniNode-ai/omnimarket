# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_lab_alarm: every setting of the broker lag, effects-held and broker-listener conditions.

Found at ``<root>/node_lab_alarm/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``.
The package ships ``overlay.example.yaml`` (the in-memory backend, synthetic values). Nothing here has
a default: an absent key refuses the load and names it, with one exception: ``effects_held`` is the
block of the effects-held verbs only, so an overlay that lacks it still loads and the broker lag read
(the verb the schedule runs) keeps reporting; the effects-held verbs refuse it themselves (OMN-23340).
``broker_listener`` is the same: the block of the broker-listener verbs only, and those verbs refuse
it themselves, and so is ``dev_tag_heartbeat``, ``lane_census``, ``lab_pass_receipt``,
``stale_indeterminate`` and ``verdict_record`` (OMN-24425): each is the block of its own verbs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_github_gateway_io import MAX_ARTIFACT_MAX_BYTES

NODE_NAME = "node_lab_alarm"


#: Whether each backend reads a broker address from the overlay.
_TAKES_BOOTSTRAP_SERVERS = {"inmemory": False, "runtime": False, "runtime_admin": True}


class ModelAlarmBackend(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``inmemory`` reads fixtures; ``runtime`` reads the lane's manifest URL and broker;
    #: ``runtime_admin`` reads committed and end offsets with the admin client only against the
    #: overlay's ``bootstrap_servers`` (no credential, no consumer, no group joined).
    backend: Literal["inmemory", "runtime", "runtime_admin"]
    #: The bus lane whose broker address and stored login the runtime backend resolves.
    lane: str = Field(min_length=1)
    #: The broker address the ``runtime_admin`` backend reads; the other backends take none.
    bootstrap_servers: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _admin_backend_declares_its_broker(self) -> ModelAlarmBackend:
        declared = self.bootstrap_servers is not None
        if _TAKES_BOOTSTRAP_SERVERS[self.backend] != declared:
            raise ValueError(
                "bootstrap_servers is required by backend runtime_admin and by no other"
            )
        return self


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


#: The conditions that run on a schedule of their own, by the verb the unit runs.
SCHEDULABLE_VERBS = (
    "dev-tag-heartbeat",
    "delegation-chain-canary",
    "work-ledger-projection-stale",
    "lane-drift",
    "container-unhealthy",
    "lab-pass-receipt",
    "stale-indeterminate",
)


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


class ModelConditionPersistence(BaseModel):
    """How many consecutive breaching runs a subject needs before it alarms: no single-run alarms."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    persist_runs: int = Field(ge=1)
    #: Where each subject's count of consecutive breaching runs is kept between runs.
    streak_path: str = Field(min_length=1)


class ModelDevTagHeartbeatSettings(BaseModel):
    """The dev-tag heartbeat condition (OMN-24400): the agent to read, the repositories it must beat for, how old a beat may be."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The deploy agent's base URL, the old config key ``agent_url``.
    agent_url: str = Field(min_length=1)
    #: The path under the base URL that serves the heartbeat document.
    endpoint_path: str = Field(min_length=1)
    #: The tag-followed repositories the agent must carry a beat for, the old ``dev_tag_heartbeat_repositories``.
    repositories: tuple[str, ...] = Field(min_length=1)
    #: How old a beat's observed_at may be; the old script's 30 minute bound (two publish periods).
    max_age_seconds: float = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    persistence: ModelConditionPersistence


class ModelDelegationChainCanarySettings(BaseModel):
    """The delegation chain canary condition (OMN-24400): which workflow's newest completed run decides, and how old it may be."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``owner/name`` of the repository whose scheduled workflow is the canary, the old config key ``repo``.
    repository: str = Field(min_length=1)
    #: The workflow file, the old ``chain_canary_workflow``.
    workflow: str = Field(min_length=1)
    #: The branch it runs on, the old ``chain_canary_branch``.
    branch: str = Field(min_length=1)
    #: The triggering events that count as a measurement; the old script admitted scheduled runs only.
    events: tuple[str, ...] = Field(min_length=1)
    #: How old the newest completed run may be: the old ``chain_canary_max_age_minutes`` (180) in seconds.
    max_age_seconds: float = Field(gt=0)
    #: Runs read per event, newest first.
    per_page: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    persistence: ModelConditionPersistence


class ModelWorkLedgerProjectionStaleSettings(BaseModel):
    """The work-ledger projection condition (OMN-24400): the ledger file against the projection's newest row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The rolling ledger file, relative to the registry root the run works from.
    ledger_path: str = Field(min_length=1)
    #: How much of the end of the file is read: the old script's 256 KiB.
    tail_bytes: int = Field(ge=1)
    #: The row types that count; a stamped row of any other type cannot make the projection look behind.
    row_types: tuple[str, ...] = Field(min_length=1)
    #: What the alarm names as its subject, the projection's table.
    projection_name: str = Field(min_length=1)
    #: How far the projection may trail the file: the old ``work_ledger_max_lag_minutes`` (30) in seconds.
    max_lag_seconds: float = Field(gt=0)
    #: How far back from the file's newest row the projection is asked for its newest row.
    projection_window_seconds: float = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    persistence: ModelConditionPersistence


class ModelLaneCensusSettings(BaseModel):
    """The lane census conditions (OMN-24425): lane_drift and container_unhealthy read one census snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The snapshot JSON file read when no command is declared; the injection seam of the old script's state-directory fixture.
    snapshot_path: str = Field(min_length=1)
    #: argv whose stdout is the snapshot; declared, the run executes it instead of reading the file.
    snapshot_command: tuple[str, ...] | None = None
    #: The oldest ``emitted_at`` accepted: the old ``census_max_age_minutes`` (120) in seconds.
    max_age_seconds: float = Field(gt=0)
    #: The command's bound: the old script's 15 seconds.
    timeout_seconds: float = Field(gt=0)


class ModelLabPassReceiptSettings(BaseModel):
    """The lab-pass receipt condition (OMN-24425): the receipt of the sha the lane runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``owner/name`` whose workflow uploads the receipt artifact, the old config key ``repo``.
    repository: str = Field(min_length=1)
    #: The workflow file that uploads the receipt artifact.
    workflow: str = Field(min_length=1)
    #: The receipt lane value; the artifact is named ``lab-pass-receipt-<lane>-<sha>``.
    lane: str = Field(min_length=1)
    #: The deploy agent's base URL; ``GET <agent_url>/health`` carries ``loaded_code_sha``, the old ``agent_url``.
    agent_url: str = Field(min_length=1)
    #: The runtime ``/ready`` URL, tried when the agent answers no sha, the old ``ready_url``.
    ready_url: str | None = None
    #: Workflow runs read at the sha, newest first.
    runs_limit: int = Field(ge=1)
    #: The largest artifact archive fetched; the gateway refuses more than its own cap, so the overlay does too.
    max_archive_bytes: int = Field(ge=1, le=MAX_ARTIFACT_MAX_BYTES)
    timeout_seconds: float = Field(gt=0)


class ModelWatchedCondition(BaseModel):
    """One condition the stale-indeterminate verb watches, and the run record that holds its runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The condition name its run record carries.
    condition: str = Field(min_length=1)
    record_path: str = Field(min_length=1)


class ModelStaleIndeterminateSettings(BaseModel):
    """The stale-indeterminate condition (OMN-24425): a watched condition that has read INDETERMINATE run after run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    watched: tuple[ModelWatchedCondition, ...] = Field(min_length=1)
    #: Consecutive INDETERMINATE runs of one condition, newest first, with no OK or ALARM between them, before it alarms.
    #: Replaces the old script's 24 hour age: a run count needs no clock and follows each condition's own schedule.
    after_runs: int = Field(ge=2)
    #: How much of the end of each run-record file is read.
    tail_bytes: int = Field(ge=1)


class ModelVerdictRecordSettings(BaseModel):
    """The run record every journaled verb appends to, in the shape the broker lag record has."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: One file: ``{"finished_at", "reports": [{"condition", "outcome", "evidence"}]}`` per line.
    path: str = Field(min_length=1)


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
    #: Read by the dev-tag heartbeat verbs alone; absent, the overlay still loads and the other verbs run.
    dev_tag_heartbeat: ModelDevTagHeartbeatSettings | None = None
    #: Read by the delegation-chain-canary verbs alone; absent, the overlay still loads and the other verbs run.
    delegation_chain_canary: ModelDelegationChainCanarySettings | None = None
    #: Read by the work-ledger-projection-stale verbs alone; absent, the overlay still loads and the other verbs run.
    work_ledger_projection_stale: ModelWorkLedgerProjectionStaleSettings | None = None
    #: Read by the lane-drift and container-unhealthy verbs alone; absent, the overlay still loads and the other verbs run.
    lane_census: ModelLaneCensusSettings | None = None
    #: Read by the lab-pass-receipt verbs alone; absent, the overlay still loads and the other verbs run.
    lab_pass_receipt: ModelLabPassReceiptSettings | None = None
    #: Read by the stale-indeterminate verbs alone; absent, the overlay still loads and the other verbs run.
    stale_indeterminate: ModelStaleIndeterminateSettings | None = None
    #: Where the journaled verbs append their runs, which the stale-indeterminate verb reads; absent, no verb journals.
    verdict_record: ModelVerdictRecordSettings | None = None
    #: One schedule per condition, keyed by the verb its unit runs (OMN-24400); the ``schedule`` block stays the broker lag's.
    #: Absent, only the broker lag has a schedule.
    condition_schedules: dict[str, ModelLabAlarmSchedule] | None = None
    #: Absent, nothing is delivered: an overlay written before the delivery existed loads and runs unchanged.
    alarm_delivery: ModelAlarmDeliveryWiring | None = None

    @model_validator(mode="after")
    def _shape(self) -> ModelLabAlarmOverlay:
        keys = [(n.package, n.node) for n in self.groups.nodes]
        if len(keys) != len(set(keys)):
            raise ValueError("a declared node is listed twice")
        schedules = self.condition_schedules or {}
        unknown = sorted(set(schedules) - set(SCHEDULABLE_VERBS))
        if unknown:
            raise ValueError(
                f"condition_schedules names {unknown}, which is not a schedulable verb"
            )
        units = [self.schedule.unit_name, *(s.unit_name for s in schedules.values())]
        labels = [
            self.schedule.launchd_label,
            *(s.launchd_label for s in schedules.values()),
        ]
        if len(units) != len(set(units)) or len(labels) != len(set(labels)):
            raise ValueError("two schedules share a unit name or a launchd label")
        return self


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelLabAlarmOverlay:
    return load_structured_overlay(NODE_NAME, ModelLabAlarmOverlay, environ)


__all__ = [
    "NODE_NAME",
    "SCHEDULABLE_VERBS",
    "ModelAlarmBackend",
    "ModelAlarmDeliveryWiring",
    "ModelBrokerListenerSettings",
    "ModelConditionPersistence",
    "ModelDeclaredNode",
    "ModelDelegationChainCanarySettings",
    "ModelDevTagHeartbeatSettings",
    "ModelEffectsHeldSettings",
    "ModelEscalationSettings",
    "ModelGroupDerivation",
    "ModelLabAlarmOverlay",
    "ModelLabAlarmSchedule",
    "ModelLabAlarmScheduleHost",
    "ModelLabPassReceiptSettings",
    "ModelLagGrowthRule",
    "ModelLagSettings",
    "ModelLaneCensusSettings",
    "ModelManifestResilience",
    "ModelManifestSettings",
    "ModelStaleIndeterminateSettings",
    "ModelVerdictRecordSettings",
    "ModelWatchedCondition",
    "ModelWorkLedgerProjectionStaleSettings",
    "load_overlay",
]

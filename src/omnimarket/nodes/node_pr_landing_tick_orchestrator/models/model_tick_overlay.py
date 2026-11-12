# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick orchestrator's deployment overlay: every setting, no code default for a deployment fact."""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.models.landing_decision import REPO_PATTERN

OVERLAY_ID = "node_pr_landing_tick_orchestrator"


class EnumTickBackend(StrEnum):
    INMEMORY = "inmemory"
    REDPANDA = "redpanda"


class ModelRerunRequestsOverlay(BaseModel):
    """The rerun-request section: the per-head limit and where requests are read (OMN-23830).

    Absent from an overlay, the tick reads no requests. With it, the limit is declared here and nowhere
    in code; backend redpanda also needs the request topic and its consumer group.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_reruns_per_head: int = Field(
        ge=0,
        description="Re-runs granted per pull request head, any job; a request past it is limit_reached.",
    )
    request_topic: str | None = Field(
        default=None,
        description="Redpanda only: the topic lanes publish rerun requests on (the contract's rerun_request.command_topic).",
    )
    consumer_group: str | None = Field(
        default=None,
        description="Redpanda only: base name of the request reader's group; the host identity is appended as for the snapshot reader.",
    )
    poll_timeout_s: float | None = Field(
        default=None,
        gt=0,
        description="Redpanda only: how long the request read waits.",
    )
    max_requests: int = Field(
        default=20, ge=1, description="Requests answered in one pass."
    )
    ledger_path: str | None = Field(
        default=None,
        description="The file the granted re-runs are kept in across passes; absent: kept in memory for the pass only.",
    )


class ModelTickOrchestratorOverlay(BaseModel):
    """<root>/node_pr_landing_tick_orchestrator/overlay.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumTickBackend = Field(
        description="Where recorded tick snapshots are read from."
    )
    publish_backend: EnumTickBackend = Field(
        default=EnumTickBackend.INMEMORY,
        description=(
            "Where the compared and compare-failed events are published: inmemory (the default, "
            "tests) or redpanda (the overlay's bus_lane or bootstrap_servers) (OMN-23539)."
        ),
    )
    act_enabled: bool = Field(
        description="Whether act mode may call the writer. False while the script keeps landing."
    )
    cutover_repository: str | None = Field(
        default=None,
        pattern=REPO_PATTERN,
        description=(
            "The one repository the act-mode writer lands (owner/name); every other "
            "repository's action is refused as out_of_cutover. Absent until the cutover "
            "rule reads true (OMN-23405)."
        ),
    )
    github_mode: EnumPrLandingGithubMode | None = Field(
        default=None,
        description=(
            "The GitHub landing effect's mode the act-mode writer sends: dry_run records, "
            "enforce writes. Required with act_enabled (OMN-23405)."
        ),
    )
    max_snapshots: int = Field(ge=1, description="Snapshots compared in one run.")
    floor_piece: int = Field(
        ge=1,
        le=18,
        description="The floor piece the emission chains are counted under.",
    )
    emission_journal: str | None = Field(
        default=None, description="Redpanda profile only: the emission journal path."
    )
    bootstrap_servers: str | None = Field(
        default=None,
        description="Redpanda only, in place of bus_lane: a plaintext lab broker started for a proof.",
    )
    bus_lane: str | None = Field(
        default=None,
        description=(
            "Redpanda only: the workspace bus lane the tick records are read from (dev); the broker "
            "and client transport resolve from the lane config under workspace_root (OMN-23458)."
        ),
    )
    workspace_root: str | None = Field(
        default=None,
        description="Redpanda with bus_lane: the workspace root whose lane config names the broker.",
    )
    consumer_group: str | None = Field(
        default=None,
        description=(
            "Redpanda only: the consumer group's base name. The group the reader joins is this name, "
            "a hyphen and the instance identity the host_env variable holds (OMN-22921), so two "
            "orchestrators on the same topic never share a group and split its records."
        ),
    )
    host_env: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Redpanda only: the environment variable holding this instance's host identity (the unit "
            "sets it from its host), appended to consumer_group. Unset or empty at run time refuses the "
            "pass; a missing identity never falls back to the shared group."
        ),
    )
    join_timeout_s: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Redpanda only: how long the consumer group join may take before the pass ends unread "
            "(measured on the dev broker, OMN-22921)."
        ),
    )
    run_limit_s: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Redpanda only: the unit's run limit in seconds (the registry unit's run_timeout_s), declared "
            "here so the pass's own bounds are checked against it: join_timeout_s plus poll_timeout_s must "
            "fit inside it with run_margin_s to spare."
        ),
    )
    run_margin_s: float | None = Field(
        default=None,
        gt=0,
        description="Redpanda only: the time a pass keeps in hand for compare and publish after its reads.",
    )
    snapshot_topic: str | None = Field(
        default=None,
        description="Redpanda only: the topic the landing snapshot step's tick records arrive on (its record_topic).",
    )
    poll_timeout_s: float | None = Field(
        default=None, gt=0, description="Redpanda only."
    )
    script_out_of_scope_repositories: tuple[str, ...] = Field(
        default=(),
        description=(
            "The repositories the old landing controller rules out of scope (the merge sweep's "
            "out_of_scope list): it reads no facts and records no decision for them, so its recorded "
            "decision for such a repository is no action. Act mode writes that record for the "
            "cutover repository when it is one of these and the tick carries none (OMN-23458)."
        ),
    )

    rerun_requests: ModelRerunRequestsOverlay | None = Field(
        default=None,
        description="The rerun-request section (OMN-23830); absent: no request is read.",
    )

    @model_validator(mode="after")
    def _act_needs_a_cutover(self) -> ModelTickOrchestratorOverlay:
        if self.act_enabled and (
            self.cutover_repository is None or self.github_mode is None
        ):
            raise ValueError(
                "act_enabled needs cutover_repository and github_mode: act mode "
                "writes for one named repository only"
            )
        if self.bus_lane and self.bootstrap_servers:
            raise ValueError("name one of bus_lane or bootstrap_servers, not both")
        if self.bus_lane and not self.workspace_root:
            raise ValueError("bus_lane needs workspace_root")
        if (
            self.publish_backend is EnumTickBackend.REDPANDA
            and not self.bus_lane
            and not self.bootstrap_servers
        ):
            raise ValueError(
                "publish_backend redpanda needs bus_lane or bootstrap_servers"
            )
        if self.backend is EnumTickBackend.REDPANDA:
            missing = [
                name
                for name in (
                    "host_env",
                    "join_timeout_s",
                    "run_limit_s",
                    "run_margin_s",
                )
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"backend redpanda needs overlay fields {missing}")
            rerun = self.rerun_requests
            if rerun is not None:
                absent = [
                    name
                    for name in ("request_topic", "consumer_group", "poll_timeout_s")
                    if getattr(rerun, name) is None
                ]
                if absent:
                    raise ValueError(
                        f"backend redpanda with rerun_requests needs rerun_requests fields {absent}"
                    )
            # the request read joins its own group and polls after the snapshot read
            budget = (
                (self.join_timeout_s or 0)
                + (self.poll_timeout_s or 0)
                + (self.run_margin_s or 0)
                + (
                    (self.join_timeout_s or 0) + (rerun.poll_timeout_s or 0)
                    if rerun is not None
                    else 0
                )
            )
            if budget > (self.run_limit_s or 0):
                raise ValueError(
                    f"join_timeout_s {self.join_timeout_s} + poll_timeout_s {self.poll_timeout_s} + "
                    f"run_margin_s {self.run_margin_s} exceed run_limit_s {self.run_limit_s}: "
                    "the unit would end the pass before it reads"
                )
        for repository in self.script_out_of_scope_repositories:
            if not re.fullmatch(REPO_PATTERN, repository):
                raise ValueError(
                    f"script_out_of_scope_repositories entry {repository!r} is not owner/name"
                )
        return self


class ModelTickTopicSelection(BaseModel):
    """Which topic the snapshot reader reads, under which group, from which broker (OMN-23458).

    The broker is a declared bus lane under a workspace root (the deployed form), or a plaintext
    lab broker started for a proof; never both.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    consumer_group: str = Field(min_length=1)
    join_timeout_s: float = Field(gt=0)
    bus_lane: str | None = None
    workspace_root: str | None = None
    bootstrap_servers: str | None = None

    @model_validator(mode="after")
    def _one_broker(self) -> ModelTickTopicSelection:
        if bool(self.bus_lane) == bool(self.bootstrap_servers):
            raise ValueError(
                "a topic selection names one of bus_lane or bootstrap_servers"
            )
        if self.bus_lane and not self.workspace_root:
            raise ValueError("bus_lane needs workspace_root")
        return self


def resolved_consumer_group(
    overlay: ModelTickOrchestratorOverlay, environ: Mapping[str, str]
) -> str:
    """The consumer group this instance joins: the overlay's base name and the host identity (OMN-22921)."""
    if not overlay.consumer_group or not overlay.host_env:
        raise ValueError("backend redpanda needs consumer_group and host_env")
    identity = environ.get(overlay.host_env, "").strip()
    if not identity:
        raise ValueError(
            f"the host identity variable {overlay.host_env} is unset or empty: "
            "a consumer group without it would be shared with another instance"
        )
    return f"{overlay.consumer_group}-{identity}"


__all__: list[str] = [
    "OVERLAY_ID",
    "EnumTickBackend",
    "ModelRerunRequestsOverlay",
    "ModelTickOrchestratorOverlay",
    "ModelTickTopicSelection",
    "resolved_consumer_group",
]

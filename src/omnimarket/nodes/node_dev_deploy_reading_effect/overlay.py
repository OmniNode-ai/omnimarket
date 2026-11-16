# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_dev_deploy_reading_effect: every setting of the piece 11 reading, none in code.

Found at ``<root>/node_dev_deploy_reading_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_dev_deploy_reading import ModelLineageRule
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_dev_deploy_reading_effect"


class ModelDevDeployPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    piece: int = Field(ge=1)
    publish_max_age_seconds: int = Field(ge=1)


class ModelDevDeployThresholds(BaseModel):
    """The plan's numbers: a PASS receipt within an hour of a merge, a probe inside 24 hours."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required_lane: str = Field(min_length=1)
    receipt_deadline_seconds: int = Field(gt=0)
    probe_max_age_seconds: int = Field(gt=0)


class ModelMergeSettings(BaseModel):
    """Which merges need a receipt: first-parent commits of ``ref`` in the clone, touching a runtime path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The variable that holds the clone's path on the host.
    clone_path_env: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    lookback_seconds: int = Field(gt=0)
    #: A commit is runtime-affecting when it changes a file matching one of these (``**`` crosses directories).
    runtime_path_globs: tuple[str, ...] = Field(min_length=1)
    git_timeout_seconds: int = Field(gt=0)


class ModelReceiptSettings(BaseModel):
    """Where the lane's lab pass receipt is read: the lab runtime's lane-health projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    projection_url: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)


class ModelDeployMergeRepo(BaseModel):
    """One repository whose dev merge the deploy source can read: its clone, its ref and its place in the build record."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_repo: str = Field(min_length=1)
    #: The variable that holds the clone's path on the host.
    clone_path_env: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    provenance_section: Literal["siblings", "build_context"]


class ModelDeployAgentSettings(BaseModel):
    """The deploy agent as the deploy source: its /health and /queue answers, and where its build records live."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The deploy agent's /health: the job in flight and the newest finished job.
    health_url: str = Field(min_length=1)
    #: The deploy agent's /queue: the commands ahead.
    queue_url: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)
    #: How long an idle deploy agent may leave a runtime-affecting merge out of the running build before it reads NOT WORKING.
    idle_grace_seconds: int = Field(gt=0)
    #: Which section of the runtime's build provenance record names the merge repository.
    provenance_section: Literal["siblings", "build_context"]
    #: The merge repository whose commit the build record names.
    source_repo: str = Field(min_length=1)
    #: Every repository the probe-merge verb may read a merge of; the hourly reading uses ``source_repo``.
    merge_repos: tuple[ModelDeployMergeRepo, ...] = Field(min_length=1)


class ModelProbeSettings(BaseModel):
    """The functional probe of the running runtime: its health answer and the commit its build recorded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    health_url: str = Field(min_length=1)
    #: Keys from the health answer's root to the boolean that says the runtime serves.
    health_ok_path: tuple[str, ...] = Field(min_length=1)
    ssh_host_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    compose_project: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    provenance_service: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    provenance_path: str = Field(pattern=r"^/[A-Za-z0-9_./-]+$")
    #: Keys from the provenance file's root to the exact commit the running build was made from.
    sha_path: tuple[str, ...] = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)


class ModelDrillFault(BaseModel):
    """The injected fault of a drill run: a runtime-affecting merge nobody receipted, old enough to be past its deadline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    synthetic_merge_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    age_seconds: int = Field(gt=0)


class ModelDevDeployScheduleHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user"]
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or absolute).
    venv: str = Field(min_length=1)


class ModelScheduleUnits(BaseModel):
    """The names of the two systemd user units; deployment facts, so they live here and not in the contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    drill_unit_name: str = Field(min_length=1)


class ModelDevDeployReadingOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_append: ModelLedgerAppendRoute
    publish: ModelDevDeployPublish
    thresholds: ModelDevDeployThresholds
    merges: ModelMergeSettings
    #: Whether a later commit's receipt or running sha stands in for the merge's own, and the bounds of it.
    lineage: ModelLineageRule
    receipts: ModelReceiptSettings
    #: The deploy source of the reading: the lane's lab pass receipts, or the deploy agent itself.
    source_rule: Literal["lab_pass_receipt", "deploy_agent"]
    #: The deploy-agent settings; the source of the reading when ``source_rule`` is deploy_agent.
    deploy_agent: ModelDeployAgentSettings
    probe: ModelProbeSettings
    #: Applied only by the drill verb, whose rows are tagged so no operational read counts them.
    drill_fault: ModelDrillFault
    injected_fault: str = Field(min_length=1)
    schedule_units: ModelScheduleUnits
    schedule_hosts: tuple[ModelDevDeployScheduleHost, ...] = ()
    unit_environment: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _shape(self) -> ModelDevDeployReadingOverlay:
        hosts = [host.name for host in self.schedule_hosts]
        if len(hosts) != len(set(hosts)):
            raise ValueError("a schedule host is declared twice")
        return self

    def overlay_hash(self) -> str:
        """Hash of what the reading depends on, so a changed threshold or source shows in the row."""
        text = json.dumps(
            {
                "thresholds": self.thresholds.model_dump(mode="json"),
                "merges": self.merges.model_dump(mode="json"),
                "lineage": self.lineage.model_dump(mode="json"),
                "receipts": self.receipts.projection_url,
                "probe": [self.probe.health_url, list(self.probe.sha_path)],
                "source_rule": self.source_rule,
                "deploy_agent": {
                    "health_url": self.deploy_agent.health_url,
                    "queue_url": self.deploy_agent.queue_url,
                },
                "max_age": self.publish.publish_max_age_seconds,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDevDeployReadingOverlay:
    return load_structured_overlay(NODE_NAME, ModelDevDeployReadingOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelDeployAgentSettings",
    "ModelDeployMergeRepo",
    "ModelDevDeployPublish",
    "ModelDevDeployReadingOverlay",
    "ModelDevDeployScheduleHost",
    "ModelDevDeployThresholds",
    "ModelDrillFault",
    "ModelMergeSettings",
    "ModelProbeSettings",
    "ModelReceiptSettings",
    "ModelScheduleUnits",
    "load_overlay",
]

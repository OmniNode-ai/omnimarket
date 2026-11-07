# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_reading_effect: every setting of the host readings, none in code.

Found at ``<root>/node_host_reading_effect/overlay.yaml`` under a root named in
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
from omnimarket.models.model_host_reading import EnumHostProbe
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_host_reading_effect"


class ModelHostReadingPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published reading stays fresh; older, it reads NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)


class ModelOutboxProbeSettings(BaseModel):
    """Where a host's queued ledger rows sit and when they are too old or refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_path_env: str = Field(min_length=1)
    lock_dirname: str = Field(min_length=1)
    outbox_suffix: str = Field(min_length=1)
    refused_dirname: str = Field(min_length=1)
    pending_glob: str = Field(min_length=1)
    queued_at_field: str = Field(min_length=1)
    oldest_max_seconds: int = Field(ge=0)
    refused_max: int = Field(ge=0)
    injected_fault: str = Field(min_length=1)


class ModelOldNameLink(BaseModel):
    """One old ledger name and the entry beside the configured ledger it must resolve to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    ledger_relative: str = Field(min_length=1)


class ModelOldNamesProbeSettings(BaseModel):
    """The old names kept as links during the move; removed with the probe at the bus cutover."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_env: str = Field(min_length=1)
    ledger_path_env: str = Field(min_length=1)
    links: tuple[ModelOldNameLink, ...] = Field(min_length=1)
    injected_fault: str = Field(min_length=1)


class ModelHostCanaryProbeSettings(BaseModel):
    """Piece 2's hourly canary: one lane per pooled host, through the route lanes use.

    The route is a command held in ``run_command_env`` (shell words; ``{host}`` is replaced by
    the host's name), to which the probe appends the canary itself. The hosts come from the lab
    host table in the overlay store; a parked host is listed and never fails the reading.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_command_env: str = Field(min_length=1)
    #: The interpreter that runs the canary on the host, appended after the route's words.
    canary_interpreter: str = Field(min_length=1)
    #: Seconds one canary may take, placement included; past it the canary reads timed-out.
    run_timeout_seconds: int = Field(ge=1)
    #: Exit codes by which the route refuses a full host with its own typed refusal.
    busy_exit_codes: tuple[int, ...]
    #: The line prefix of the canary's typed result.
    result_marker: str = Field(min_length=1)
    #: The line prefix the lane wrapper prints when it ends; empty when the route has no wrapper.
    wrapper_end_marker: str
    #: The text by which the route's pool read says a host is admitted, after ``<host>: ``.
    pool_admitted_marker: str = Field(min_length=1)
    #: The text by which the route's pool read says a host could not be read.
    pool_unreadable_marker: str = Field(min_length=1)
    #: Texts by which the route's pool read refuses a host for a reason of the host table or the
    #: load bar (out of rotation, job kinds, over the bar, hot, short of memory), after ``<host>: ``.
    #: Such a host ends parked: listed with the refusal, never a failed host. Empty parks none.
    pool_parked_markers: tuple[str, ...]
    #: Paths read on the host for free disk (``~`` expands there); the smallest counts.
    disk_paths: tuple[str, ...] = Field(min_length=1)
    disk_floor_gb: float = Field(ge=0)
    #: The variable holding the command that prints the newest capacity advertisement of every
    #: host as JSON; null when no host principal may read the advertisements yet.
    advertisements_command_env: str | None
    advertisements_timeout_seconds: int = Field(ge=1)
    #: An advertisement older than this, or none, fails its host.
    advert_max_age_seconds: int = Field(ge=1)
    #: A drill overlay sets this, so its rows carry drill=yes and never count as an operational
    #: reading (the floor counts them as the piece's error events; the reading writer skips them).
    drill: bool
    injected_fault: str = Field(min_length=1)


class ModelJoinJ1Node(BaseModel):
    """One node the host stage needs the graph's verdict for, and where its modules sit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    #: Repository-relative directory holding the node's modules.
    package_dir: str = Field(min_length=1)


class ModelJoinJ1Emission(BaseModel):
    """The emission journal the floor reads its receipts from, relative to a directory an environment variable names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    journal_relpath: str = Field(min_length=1)


class ModelJoinJ1Settings(BaseModel):
    """Join J1, the code graph to hosts and capacity: which graph run it reads and what the host stage must find in it.

    The join emits its two chains under the graph run's correlation id: the graph run's own (``graph_chain_node``,
    ``from_piece``) and the host stage's (``host_chain_node``, ``to_piece``), so the floor judges the join from them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    join: str = Field(pattern=r"^J\d{1,2}$")
    from_piece: int = Field(ge=1, le=18)
    to_piece: int = Field(ge=1, le=18)
    graph_chain_node: str = Field(min_length=1)
    host_chain_node: str = Field(min_length=1)
    #: The nodes host discovery and placement depend on; a graph run must carry a verdict for each.
    nodes: tuple[ModelJoinJ1Node, ...] = Field(min_length=1)
    #: Absent means the join publishes its row only and emits no chains.
    emission: ModelJoinJ1Emission | None = None
    injected_fault: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelJoinJ1Settings:
        if self.from_piece == self.to_piece:
            raise ValueError("a join joins two pieces")
        names = [node.name for node in self.nodes]
        if len(names) != len(set(names)):
            raise ValueError("a join node is declared twice")
        return self


class ModelHostReadingHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user", "launchd"]
    probes: tuple[EnumHostProbe, ...] = Field(min_length=1)
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or absolute). A
    #: dedicated venv of a worktree or a release, never a dispatch venv.
    venv: str = Field(min_length=1)
    #: Environment the host's installed unit carries (the routes and their roots).
    environment: dict[str, str]


class ModelHostReadingOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ledger_append: ModelLedgerAppendRoute
    publish: ModelHostReadingPublish
    outbox: ModelOutboxProbeSettings
    old_names: ModelOldNamesProbeSettings | None
    #: Absent means no host declares the host-canary probe.
    canary: ModelHostCanaryProbeSettings | None = None
    #: Absent means no host declares the join-j1 probe.
    join_j1: ModelJoinJ1Settings | None = None
    hosts: tuple[ModelHostReadingHost, ...]

    @model_validator(mode="after")
    def _shape(self) -> ModelHostReadingOverlay:
        names = [host.name for host in self.hosts]
        if len(names) != len(set(names)):
            raise ValueError("a host is declared twice")
        for host in self.hosts:
            if len(host.probes) != len(set(host.probes)):
                raise ValueError(f"host {host.name} declares a probe twice")
            if EnumHostProbe.LEDGER_OLD_NAMES in host.probes and not self.old_names:
                raise ValueError(
                    f"host {host.name} declares ledger-old-names with no old_names settings"
                )
            if EnumHostProbe.HOST_CANARY in host.probes and not self.canary:
                raise ValueError(
                    f"host {host.name} declares host-canary with no canary settings"
                )
            if EnumHostProbe.JOIN_J1 in host.probes and not self.join_j1:
                raise ValueError(
                    f"host {host.name} declares join-j1 with no join_j1 settings"
                )
        return self

    def host(self, name: str) -> ModelHostReadingHost | None:
        return next((host for host in self.hosts if host.name == name), None)

    def overlay_hash(self) -> str:
        """Hash of what the readings depend on, so a threshold change shows in the row."""
        text = json.dumps(
            {
                "outbox": self.outbox.model_dump(mode="json"),
                "old_names": self.old_names.model_dump(mode="json")
                if self.old_names
                else None,
                **(
                    {"canary": self.canary.model_dump(mode="json")}
                    if self.canary
                    else {}
                ),
                **(
                    {"join_j1": self.join_j1.model_dump(mode="json")}
                    if self.join_j1
                    else {}
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelHostReadingOverlay:
    return load_structured_overlay(NODE_NAME, ModelHostReadingOverlay, environ)

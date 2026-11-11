# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The contract's ``runtime`` block and the deployment overlay the runtime entry reads."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_factory_floor_input import (
    ModelFloorMachineDecl,
    ModelFloorProcessDecl,
)
from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetDeclaration,
)


class ModelFactoryFloorSources(BaseModel):
    """Where each source is, relative to the registry home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    watcher_relpath: str = Field(min_length=1)
    plans_today_dir: str = Field(min_length=1)
    plans_today_file: str = Field(min_length=1)
    linear_snapshot_glob: str = Field(min_length=1)
    placement_relpath: str = Field(min_length=1)
    friction_report_relpath: str = Field(min_length=1)
    linear_titles_glob: str = Field(min_length=1)
    runner_fleet_relpath: str = Field(min_length=1)
    # The host-drift fleet projection node_host_drift_fleet folds (OMN-21867); the Machines tab reads each host's heartbeat from it.
    host_drift_relpath: str = Field(min_length=1)
    # The required contexts the queue-status tick saved: a map keyed by repository (or ``repository:branch``).
    required_contexts_relpath: str = Field(min_length=1)
    # The record node_factory_floor_unresolved_effect writes: the ledger rows older than the window that nothing resolved (OMN-19998).
    unresolved_rows_relpath: str = Field(min_length=1)


class ModelFactoryFloorSourceBars(BaseModel):
    """Minutes after which a source reads ``stale``. One key per source the line board depends on; each is required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plans: int = Field(gt=0)
    watcher: int = Field(gt=0)
    ledger: int = Field(gt=0)
    plans_today: int = Field(gt=0)
    linear: int = Field(gt=0)
    running_evidence: int = Field(gt=0)
    required_contexts: int = Field(gt=0)
    goal: int = Field(gt=0)
    placement: int = Field(gt=0)


class ModelFactoryFloorLaneSources(BaseModel):
    """Where the live-lane and machine-bay sources are (OMN-21733).

    ``*_env`` names an environment variable that, when set, gives the directory or file; else the ``*_fallback`` is
    taken relative to the user's home (``host_table_relpath`` relative to the registry home).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    remote_lane_dir_env: str = Field(min_length=1)
    remote_lane_dir_fallback: str = Field(min_length=1)
    pool_receipt: str = Field(min_length=1)
    receipt_glob: str = Field(min_length=1)
    receipt_window_hours: int = Field(gt=0)
    # How many lane directories are examined at once: the scan is one metadata read per directory and the directory
    # holds tens of thousands, so one at a time is minutes under the Background clamp the module tick runs in.
    receipt_scan_workers: int = Field(gt=0)
    result_chars: int = Field(gt=0)
    placement_dir_env: str = Field(min_length=1)
    placement_dir_fallback: str = Field(min_length=1)
    lane_marker_min_id: int = Field(gt=0)
    host_table_env: str = Field(min_length=1)
    controller_dir_env: str = Field(min_length=1)
    controller_dir_fallback: str = Field(min_length=1)
    process_start_grace_seconds: int = Field(ge=0)


class ModelFactoryFloorCloudSource(BaseModel):
    """The existing cloud node's read operation, called through the canonical runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    node_name: str = Field(min_length=1)
    action: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)


class ModelFactoryFloorLimitSources(BaseModel):
    """Limits status mirrors, relative to the registry home (OMN-22010)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_meter_relpath: str = Field(min_length=1)
    engines_relpath: str = Field(min_length=1)
    token_pool_glob: str = Field(min_length=1)


class ModelFactoryFloorContainerSource(BaseModel):
    """How a host's container listing is read: ssh to the overlay's target, then the listing command (one JSON object per line)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ssh_options: tuple[str, ...] = Field(min_length=1)
    # `docker ps` with an explicit field list: the default `{{json .}}` makes the daemon compute every container's size.
    listing_command: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0)


class ModelFactoryFloorOutput(BaseModel):
    """The path the JSON is written to, relative to the registry home."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relpath: str = Field(min_length=1)


class ModelFactoryFloorRuntime(BaseModel):
    """The ``runtime`` block of contract.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_env: str = Field(min_length=1)
    plans_kb_env: str = Field(min_length=1)
    overlay_file_env: str = Field(min_length=1)
    # The landing goal's scope overlay (the one the simple floor reads): the stopped-lane list needs it, and lists none unread.
    goal_scope_overlay_id: str = Field(min_length=1)
    goal_scope_file_env: str = Field(min_length=1)
    goal_ticket_rule_overlay_id: str = Field(min_length=1)
    goal_ticket_rule_file_env: str = Field(min_length=1)
    plugin_root_env: str = Field(min_length=1)
    phoenix_url_env: str = Field(min_length=1)
    ledger_window_days: int = Field(gt=0)
    ledger_cut_chars: int = Field(gt=0)
    ledger_timeout_seconds: int = Field(gt=0)
    # The row kinds the effect keeps from the window.
    ledger_kinds: tuple[str, ...] = Field(min_length=1)
    # Hours after which the unresolved step's record reads unread (its own margin past the window's edge is the other bound).
    unresolved_max_age_hours: int = Field(gt=0)
    source_max_age_min: ModelFactoryFloorSourceBars
    # The plans repository file with the goal's rows, and the verdicts that close a row.
    goal_relpath: str = Field(min_length=1)
    goal_closed_verdicts: tuple[str, ...] = Field(min_length=1)
    watcher_max_age_min: int = Field(gt=0)
    manifest_suffix: str = Field(min_length=1)
    plan_header_keys: tuple[str, ...] = Field(min_length=1)
    plans_relpath: str = Field(min_length=1)
    heading_task_patterns: tuple[str, ...] = Field(min_length=1)
    sources: ModelFactoryFloorSources
    lane_sources: ModelFactoryFloorLaneSources
    cloud_source: ModelFactoryFloorCloudSource
    limit_sources: ModelFactoryFloorLimitSources
    container_source: ModelFactoryFloorContainerSource
    output: ModelFactoryFloorOutput


class ModelFactoryFloorEffectContract(BaseModel):
    """The ``runtime`` block is the only part of this contract the handler reads."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    runtime: ModelFactoryFloorRuntime


class ModelFactoryFloorPlanRef(BaseModel):
    """A plan that is not on the plans repository's default branch yet: read from this ref until it is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    ref: str = Field(min_length=1)


class ModelFactoryFloorTaskLinks(BaseModel):
    """The tickets that carry a plan's tasks, for a plan whose own text names none (a task id maps to its tickets)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path_contains: str = Field(min_length=1)
    tasks: dict[str, tuple[str, ...]] = Field(min_length=1)


class ModelFactoryFloorContainerHost(BaseModel):
    """A machine whose lane containers are read: the name the budget declarations use and the ssh target that reaches it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1)
    ssh_target: str = Field(min_length=1)


class ModelFactoryFloorOverlay(BaseModel):
    """Deployment choice: which plans are lines and which machines are bays. There is no packaged default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path_contains: tuple[str, ...] = Field(min_length=1)
    # Optional argv prefix ending in `onex node`, e.g. ssh to the reachable state owner.
    # Empty calls the node on the publisher. Paths are resolved by the node on its own host.
    cloud_accounts_node_command: tuple[str, ...] = ()
    # The machines drawn as bays, in order. A machine not listed here is never drawn, whatever the pool read holds.
    machines: tuple[ModelFloorMachineDecl, ...] = ()
    # The shared lab host table (caps, repair), relative to the registry home, for a deployment where the host_table_env
    # variable is not set. Empty: the host table is unread unless the variable names it.
    host_table_relpath: str = ""
    # Plans on a branch that has not merged: a line drawn from the ref until the plan reaches the default branch.
    plan_refs: tuple[ModelFactoryFloorPlanRef, ...] = ()
    # Which tickets carry which task of a plan whose text names none.
    plan_task_links: tuple[ModelFactoryFloorTaskLinks, ...] = ()
    # The declared line (a plan_path_contains text) that shows each lane's application containers; empty shows none.
    container_read_line: str = ""
    container_hosts: tuple[ModelFactoryFloorContainerHost, ...] = ()
    container_budgets: tuple[ModelLaneContainerBudgetDeclaration, ...] = ()
    # The ledger reader's row source for the window read (its ``--source``: file, db or compare; the reader refuses another).
    # None names no source and leaves the reader its own default, which on a host whose ledger record is the bus is the bus read.
    ledger_source: str | None = Field(default=None, min_length=1)
    # The line board's declarations (OMN-22368). No default: an overlay without them is refused by name.
    processes: tuple[ModelFloorProcessDecl, ...]
    roles: tuple[str, ...]
    release_required_repos: tuple[str, ...]
    held_gates: tuple[str, ...]

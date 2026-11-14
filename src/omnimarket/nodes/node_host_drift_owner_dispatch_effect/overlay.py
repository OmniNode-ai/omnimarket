# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_host_drift_owner_dispatch_effect: every bound, actor, path and command of the pass (OMN-24239).

Found at ``<root>/node_host_drift_owner_dispatch_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` (the in-memory backend,
synthetic values). Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.model_alarm_owner_responder import (
    ModelAlarmOwnerPolicy,
)
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)

NODE_NAME = "node_host_drift_owner_dispatch_effect"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelAlarmCells(_Frozen):
    """The cells of the FRICTION row an open drift item is rendered as, for the responder to read."""

    lane: str = Field(min_length=1)
    #: The actor the responder's policy reads rows of (the same value as ``policy.alarm_actor``).
    actor: str = Field(min_length=1)
    friction_class: str = Field(min_length=1)
    #: The condition the responder's policy lists in ``policy.conditions``.
    condition: str = Field(min_length=1)
    ticket: str = Field(pattern=r"^OMN-\d+$")
    evidence_limit: int = Field(ge=1)


class ModelLiveLanesSettings(_Frozen):
    """How the lanes already live are read from the rolling ledger, over the bus."""

    #: The ledger query command (argv); the node adds ``--query rows --kind <K> --term <T> --since <S> --limit <N>``.
    query_command: tuple[str, ...] = Field(min_length=1)
    lookback_hours: float = Field(gt=0)
    row_limit: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)


class ModelDispatchSettings(_Frozen):
    """The remote-lane runner the pass starts a lane with."""

    interpreter: str = Field(min_length=1)
    #: The environment variable that holds the path of the runner script, set by the unit's declaration.
    runner_env: str = Field(min_length=1)
    subcommand: str = Field(min_length=1)
    model: str = Field(min_length=1)
    ticket: str = Field(pattern=r"^OMN-\d+$")
    #: Directory (relative to the working directory) the briefs and runner logs are written to.
    briefs_dir: str = Field(min_length=1)
    #: A runner still running after this many seconds is a lane that started.
    settle_seconds: float = Field(gt=0)
    extra_args: tuple[str, ...]


class ModelBriefSections(_Frozen):
    """The lines of the task part of a fix lane's brief that the lane-brief check and the lane read (one value each)."""

    exemplars: str = Field(min_length=1)
    seams: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    delegation: str = Field(min_length=1)


class ModelBriefSettings(_Frozen):
    """How a fix lane's brief is built and checked: the same lane-brief path the lab fill's briefs take."""

    #: The lane-brief script and the standing rules block script, relative to the omni plugin root.
    lane_brief_script: str = Field(min_length=1)
    rules_block_script: str = Field(min_length=1)
    #: The plugin root is this many parent directories above the remote-lane runner script file (the unit names the
    #: runner): 4 for ``<root>/skills/remote-lane/scripts/onex_remote_lane.py``.
    runner_root_levels: int = Field(ge=1)
    #: The environment variable that holds the ledger file path the lane-brief reads and the check re-reads.
    ledger_env: str = Field(min_length=1)
    #: The lane the fix lane reports to, and the landing lane, ruling lane and ruling stamp section 3a cites.
    parent: str = Field(min_length=1)
    landing_lane: str = Field(min_length=1)
    landing_ruling_lane: str = Field(min_length=1)
    landing_ruling: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    out_of_scope: str = Field(min_length=1)
    #: The repository the lane works in for the brief's pre-push contract step (``none`` for a lane that names its own).
    repo: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    #: The ROUTE band and its dims (``S2A1R1V2N1C1J1``); the ROUTE score is the sum of the dims.
    route_band: str = Field(pattern=r"^B\d$")
    route_dims: str = Field(pattern=r"^(?:[A-Z]\d){7}$")
    #: Where the check runs (the lane-brief ``--where`` value).
    check_where: str = Field(min_length=1)
    #: Each lane-brief command is given this long.
    command_timeout_seconds: float = Field(gt=0)
    #: A regeneration of the 3a block after a ledger row landed between the read and the check.
    regenerate_attempts: int = Field(ge=1)
    sections: ModelBriefSections


class ModelStoreEnvSetting(_Frozen):
    """One environment name the brief check reads, filled from the secret store when the unit's environment has none."""

    env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    #: The store entry name the value is read from.
    secret: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")


class ModelBriefEnvironment(_Frozen):
    """The environment names the brief check reads and where their values come from when the unit's environment has none."""

    #: The store folder the settings' values are read from.
    folder: str = Field(min_length=1, pattern=r"^/")
    settings: tuple[ModelStoreEnvSetting, ...] = Field(min_length=1)


class ModelDriftOwnerOverlay(_Frozen):
    #: ``inmemory`` records instead of reading files, the ledger and starting lanes; ``runtime`` does all three.
    backend: Literal["inmemory", "runtime"]
    #: The host drift checker's state file.
    state_path: str = Field(min_length=1)
    #: Cause key -> stamp of the last started dispatch, kept between passes.
    dispatch_record_path: str = Field(min_length=1)
    #: Items of these kinds are the liveness watchdog's, not an owner's (run staleness).
    excluded_kinds: tuple[str, ...]
    excluded_keys: tuple[str, ...]
    alarm: ModelAlarmCells
    policy: ModelAlarmOwnerPolicy
    live_lanes: ModelLiveLanesSettings
    dispatch: ModelDispatchSettings
    brief: ModelBriefSettings
    #: The secret store the brief environment's values are read from when the unit's environment has none.
    secret_store: ModelSecretStoreOverlay
    #: The environment names the brief check reads and where their values come from when the unit's environment has none.
    brief_environment: ModelBriefEnvironment


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelDriftOwnerOverlay:
    return load_structured_overlay(NODE_NAME, ModelDriftOwnerOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelAlarmCells",
    "ModelBriefEnvironment",
    "ModelBriefSections",
    "ModelBriefSettings",
    "ModelDispatchSettings",
    "ModelDriftOwnerOverlay",
    "ModelLiveLanesSettings",
    "ModelStoreEnvSetting",
    "load_overlay",
]

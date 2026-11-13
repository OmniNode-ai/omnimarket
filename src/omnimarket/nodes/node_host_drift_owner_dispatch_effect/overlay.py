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
from omnimarket.nodes.node_alarm_owner_responder_compute.models.model_alarm_owner_responder import (
    ModelAlarmOwnerPolicy,
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


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelDriftOwnerOverlay:
    return load_structured_overlay(NODE_NAME, ModelDriftOwnerOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelAlarmCells",
    "ModelDispatchSettings",
    "ModelDriftOwnerOverlay",
    "ModelLiveLanesSettings",
    "load_overlay",
]

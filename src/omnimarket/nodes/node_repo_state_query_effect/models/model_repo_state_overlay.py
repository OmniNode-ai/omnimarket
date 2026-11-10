# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_repo_state_query_effect: every deployment setting, none in code (OMN-23033).

Found at ``<root>/node_repo_state_query_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``, or the file ``ONEX_REPO_STATE_OVERLAY`` names. The package
ships ``overlay.example.yaml`` with synthetic values. No key has a default: an absent key
refuses the load and names it. Deployment facts that differ per host (where the clones
sit, the watcher state file, the sync command) are named by the environment variables
the overlay lists, so one overlay serves every host.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

OVERLAY_ID = "node_repo_state_query_effect"
OVERLAY_FILE_ENV = "ONEX_REPO_STATE_OVERLAY"


class ModelRepoStateBusProfile(BaseModel):
    """One bus profile: the in-memory default, or the Redpanda (Kafka) bus of a lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["inmemory", "kafka"]
    lane: str | None = Field(default=None, min_length=1)
    kafka_bootstrap_env: str | None = Field(default=None, min_length=1)
    #: Environment variable naming the workspace root that holds the lane declaration
    #: (``config/lab/ci_bus_lanes.yaml``); a named lane cannot be resolved without it.
    workspace_root_env: str | None = Field(default=None, min_length=1)


class ModelRepoStateUnitHost(BaseModel):
    """One host that runs the serve process, and the scheduler that keeps it running."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user"]


class ModelRepoStateUnit(BaseModel):
    """The unit declaration of ``repo-state serve``: what the installer renders, per host.

    ``environment`` values may start from ``$OMNI_HOME`` or ``$HOME``; the installing host
    fills both, so the overlay names no home directory and one document serves every host.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    #: The bus profile the unit serves on (a key of ``profiles``).
    bus_profile: str = Field(min_length=1)
    #: The interpreter the unit runs, from ``$OMNI_HOME`` or ``$HOME``.
    python: str = Field(min_length=1)
    #: Directories put on ``PYTHONPATH`` ahead of the interpreter's own install, from ``$OMNI_HOME``.
    pythonpath: list[str]
    restart_seconds: int = Field(ge=1)
    stop_timeout_seconds: int = Field(ge=1)
    #: The unit's log file, from ``$OMNI_HOME``.
    log_file: str = Field(min_length=1)
    environment: dict[str, str] = Field(min_length=1)
    hosts: list[ModelRepoStateUnitHost] = Field(min_length=1)


class ModelRepoStateOverlay(BaseModel):
    """Every setting the serve process and the query caller read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Environment variable naming the directory that holds this host's canonical clones.
    clones_root_env: str = Field(min_length=1)
    #: Environment variable naming the PR watcher's state file on this host.
    watcher_state_env: str = Field(min_length=1)
    #: Environment variable naming the directory the bus-state fold persists under.
    state_dir_env: str = Field(min_length=1)
    bus_state_file: str = Field(min_length=1)
    #: Environment variable holding this host's clone sync command (shell words, ``{repo}``).
    sync_command_env: str = Field(min_length=1)
    #: Environment variable holding this host's watcher refresh command (``{repo}``).
    watcher_refresh_command_env: str = Field(min_length=1)
    git_timeout_seconds: float = Field(gt=0)
    pull_timeout_seconds: float = Field(gt=0)
    max_file_chars: int = Field(ge=1)
    reply_timeout_seconds: float = Field(gt=0)
    #: The bus profiles by name; ``default_profile`` is the one used when none is named.
    profiles: dict[str, ModelRepoStateBusProfile] = Field(min_length=1)
    default_profile: str = Field(min_length=1)
    #: The unit declaration the installer renders (``repo-state unit``).
    unit: ModelRepoStateUnit

    @model_validator(mode="after")
    def _unit_matches_overlay(self) -> ModelRepoStateOverlay:
        if self.unit.bus_profile not in self.profiles:
            raise ValueError(
                f"unit.bus_profile {self.unit.bus_profile!r} is not a profile; "
                f"the overlay has {sorted(self.profiles)}"
            )
        named = (
            self.clones_root_env,
            self.watcher_state_env,
            self.state_dir_env,
            self.sync_command_env,
        )
        missing = [name for name in named if name not in self.unit.environment]
        if missing:
            raise ValueError(
                f"unit.environment gives no value for {missing}, which the overlay names"
            )
        profile = self.profiles[self.unit.bus_profile]
        if profile.workspace_root_env and (
            profile.workspace_root_env not in self.unit.environment
        ):
            raise ValueError(
                f"unit.environment gives no value for {profile.workspace_root_env}, "
                f"which profile {self.unit.bus_profile!r} names as its workspace root"
            )
        return self

    def profile(self, name: str | None) -> ModelRepoStateBusProfile:
        key = name or self.default_profile
        if key not in self.profiles:
            raise ValueError(
                f"no bus profile {key!r}; the overlay has {sorted(self.profiles)}"
            )
        return self.profiles[key]


__all__ = [
    "OVERLAY_FILE_ENV",
    "OVERLAY_ID",
    "ModelRepoStateBusProfile",
    "ModelRepoStateOverlay",
    "ModelRepoStateUnit",
    "ModelRepoStateUnitHost",
]

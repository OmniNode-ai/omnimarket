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

from pydantic import BaseModel, ConfigDict, Field

OVERLAY_ID = "node_repo_state_query_effect"
OVERLAY_FILE_ENV = "ONEX_REPO_STATE_OVERLAY"


class ModelRepoStateBusProfile(BaseModel):
    """One bus profile: the in-memory default, or the Redpanda (Kafka) bus of a lane."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["inmemory", "kafka"]
    lane: str | None = Field(default=None, min_length=1)
    kafka_bootstrap_env: str | None = Field(default=None, min_length=1)


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
]

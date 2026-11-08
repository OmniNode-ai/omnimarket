# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_landing_canary_effect: every setting of the landing canary, none in code.

Found at ``<root>/node_landing_canary_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with synthetic values.
Nothing here has a default: an absent key refuses the load and names it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_landing_canary_effect"


class ModelCanaryScm(BaseModel):
    """How the ``scm`` backend writes the test pull request and reads it back.

    Every command is named by an environment variable, so a deployment's tools are never a literal here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Environment variable holding the path of a clone of the test repository.
    clone_dir_env: str = Field(min_length=1)
    #: Environment variable holding the command that opens a pull request (the ``onex scm pr-create`` form).
    pr_create_command_env: str = Field(min_length=1)
    #: Environment variable holding the command that prints a pull request's watcher state as JSON.
    pr_state_command_env: str = Field(min_length=1)
    command_timeout_seconds: int = Field(ge=1)


class ModelCanaryTarget(BaseModel):
    """The test repository, the pull request the canary opens in it and the bar it must land inside."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``inmemory`` answers from a fixture; ``scm`` writes and reads a real test repository.
    backend: Literal["inmemory", "scm"]
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base: str = Field(min_length=1)
    branch_prefix: str = Field(pattern=r"^[A-Za-z0-9_.-]+/?$")
    #: The pull request title; the nonce is appended.
    title_prefix: str = Field(min_length=1)
    #: The bar: the canary must land inside this long, or the reading is NOT WORKING.
    landing_bound_seconds: int = Field(ge=1)
    #: How often the pull request's state is read while waiting.
    poll_seconds: int = Field(ge=1)
    scm: ModelCanaryScm | None = None


class ModelCanaryPublish(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: The piece this reading is for (landing and merge queue).
    piece: int = Field(ge=1, le=18)
    #: A drill run's row is marked ``tag=drill`` and never counts as an operational reading.
    drill: bool
    #: How long a published reading stays fresh: the daily cadence plus the run bound; older, NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)
    #: How many times the row's append is tried before the run is failed; 1 tries once. Only an ``error`` or
    #: ``pending`` answer is tried again (a ``refused`` row would be refused again), and every attempt carries the
    #: same request id, so an append that landed after its caller gave up is answered ``duplicate``.
    append_attempts: int = Field(ge=1, le=10)
    #: How long to wait between two attempts of the append.
    append_retry_wait_seconds: int = Field(ge=0)


class ModelCanaryHost(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user", "launchd"]
    #: The venv whose interpreter runs the installed unit, relative to the registry root.
    venv: str = Field(min_length=1)
    #: Environment the host's unit carries (the routes and their roots); ``$OMNI_HOME`` and ``$HOME`` are filled.
    environment: dict[str, str]


class ModelLandingCanaryOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target: ModelCanaryTarget
    ledger_append: ModelLedgerAppendRoute
    publish: ModelCanaryPublish
    hosts: tuple[ModelCanaryHost, ...]
    injected_fault: str = Field(min_length=1)

    def host(self, name: str) -> ModelCanaryHost | None:
        for candidate in self.hosts:
            if candidate.name == name:
                return candidate
        return None

    def overlay_hash(self) -> str:
        """Hash of what the verdict depends on, so a bar change shows in the row."""
        text = json.dumps(
            {
                "repo": self.target.repo,
                "base": self.target.base,
                "landing_bound_seconds": self.target.landing_bound_seconds,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelLandingCanaryOverlay:
    return load_structured_overlay(NODE_NAME, ModelLandingCanaryOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelCanaryHost",
    "ModelCanaryPublish",
    "ModelCanaryScm",
    "ModelCanaryTarget",
    "ModelLandingCanaryOverlay",
    "load_overlay",
]

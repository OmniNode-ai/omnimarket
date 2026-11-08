# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_job_start_source_effect: every setting of the job-start source (OMN-22897).

Found at ``<root>/node_job_start_source_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships three examples with synthetic values: an in-memory
source and store (the default way to run it), a ledger store and a GitHub source. Nothing here has a
default: an absent key refuses the load and names it. The token is never stored: ``token_env`` names
the environment variable that holds it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_job_start_source_effect.models.model_job_start import (
    EnumJobStartFailure,
    ModelJobStart,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_job_start_source_effect"


class ModelJobStartSettings(BaseModel):
    """What is read and how a read is judged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The repositories whose job starts are read, as ``owner/name``.
    repositories: tuple[str, ...] = Field(min_length=1)
    #: A read covers the jobs that started in this many seconds before its ``now``.
    window_seconds: int = Field(ge=60)
    #: Runs created this long before the window opened are still asked for their jobs (a job can
    #: start long after its run was created).
    run_lookback_seconds: int = Field(ge=0)
    #: A read older than this at judging time reads UNKNOWN.
    max_age_seconds: int = Field(ge=1)
    #: The runner groups a public-origin start must never be seen on.
    watched_runner_groups: tuple[str, ...] = Field(min_length=1)
    #: Repositories whose public starts on a watched group are known and dated (a fork start never is).
    baseline_public_repositories: tuple[str, ...]

    @model_validator(mode="after")
    def _shape(self) -> ModelJobStartSettings:
        if len(self.repositories) != len(set(self.repositories)):
            raise ValueError("a repository is listed twice")
        return self


class ModelInMemoryFixture(BaseModel):
    """The starts one in-memory read returns (or the failure it ends in)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    starts: tuple[ModelJobStart, ...]
    #: How many seconds before the request's ``now`` the fixture was read.
    read_age_seconds: int = Field(ge=0)
    #: The repositories the fixture counts as read; when absent, every declared repository.
    repositories_read: tuple[str, ...] | None = None
    read_failure: EnumJobStartFailure | None = None
    failed_repository: str | None = None


class ModelInMemorySource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory"]
    fixture: ModelInMemoryFixture


class ModelGithubSource(BaseModel):
    """The GitHub source: the REST API, read only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["github"]
    api_base: str = Field(min_length=1)
    #: The NAME of the environment variable holding the read token; the value is never stored.
    token_env: str = Field(min_length=1)
    #: Calls one read may make; the call past it is not made.
    max_calls: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    page_size: int = Field(ge=1, le=100)
    #: Pages one listing may read; more pages than this ends the read in ``window-truncated``.
    max_pages_per_repository: int = Field(ge=1)


ModelJobStartSource = Annotated[
    ModelInMemorySource | ModelGithubSource, Field(discriminator="backend")
]


class ModelInMemoryStore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory"]


class ModelLedgerStore(BaseModel):
    """The ledger store: one STATUS row per read, appended through the configured command."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["ledger"]
    ledger_append: ModelLedgerAppendRoute
    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a stored read stays fresh; older, a reader treats it as NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)
    injected_fault: str = Field(min_length=1)


ModelJobStartStoreSetting = Annotated[
    ModelInMemoryStore | ModelLedgerStore, Field(discriminator="backend")
]


class ModelJobStartOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    settings: ModelJobStartSettings
    source: ModelJobStartSource
    store: ModelJobStartStoreSetting

    def overlay_hash(self) -> str:
        """Hash of what a read depends on, so a settings change shows in the stored row."""
        text = json.dumps(
            {
                "settings": self.settings.model_dump(mode="json"),
                "source": self.source.model_dump(mode="json", exclude={"fixture"}),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelJobStartOverlay:
    return load_structured_overlay(NODE_NAME, ModelJobStartOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelGithubSource",
    "ModelInMemoryFixture",
    "ModelInMemorySource",
    "ModelInMemoryStore",
    "ModelJobStartOverlay",
    "ModelJobStartSettings",
    "ModelJobStartSource",
    "ModelJobStartStoreSetting",
    "ModelLedgerStore",
    "load_overlay",
]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_runner_group_exposure_effect: every setting of the exposure probe.

Found at ``<root>/node_runner_group_exposure_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships two examples with synthetic values: an in-memory
source (the default way to run it) and a GitHub source. Nothing here has a default: an absent key
refuses the load and names it. The token is never stored: ``token_env`` names the environment
variable that holds it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.runner_group_exposure import (
    EnumExposureReadFailure,
    ModelExposureSettings,
    ModelGroupJob,
    ModelGroupWorkflowFile,
    ModelRunnerGroupFacts,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelLedgerAppendRoute

NODE_NAME = "node_runner_group_exposure_effect"


class ModelInMemoryFixture(BaseModel):
    """The facts one in-memory read returns (or the failure it ends in)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group: ModelRunnerGroupFacts | None = None
    files: tuple[ModelGroupWorkflowFile, ...] = ()
    jobs: tuple[ModelGroupJob, ...] = ()
    #: How many seconds before the request's ``now`` the fixture was read.
    read_age_seconds: int = Field(ge=0)
    read_failure: EnumExposureReadFailure | None = None


class ModelInMemorySource(BaseModel):
    """The in-memory source: a fixture in the overlay."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory"]
    fixture: ModelInMemoryFixture


class ModelTokenMint(BaseModel):
    """The GitHub App the source MINTS an installation token from, once per read.

    Every field is the NAME of a secret-store reference, never a value.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The org the App is installed on.
    org: str = Field(min_length=1)
    #: The NAME of the secret-store reference holding the App id; never a value.
    app_id_secret: str = Field(min_length=1)
    #: The NAME of the secret-store reference holding the App private key; never a value.
    private_key_secret: str = Field(min_length=1)


class ModelGithubSource(BaseModel):
    """The GitHub source: the REST API, read only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["github"]
    api_base: str = Field(min_length=1)
    org: str = Field(min_length=1)
    group_id: int
    #: Labels a job's runs-on names to select the group.
    group_labels: tuple[str, ...] = Field(min_length=1)
    #: The NAME of the environment variable holding the read token; the value is never stored.
    #: Read only when ``token_mint`` is null.
    token_env: str = Field(min_length=1)
    #: When set, the source MINTS a fresh installation token once per read instead of reading
    #: ``token_env``; the fields name secret-store references, never values.
    token_mint: ModelTokenMint | None
    #: The NAME of the App permission the organization read of the group needs, named in a
    #: refusal; never a value.
    org_read_permission: str = Field(min_length=1)
    #: Calls one read may make; the call past it is not made.
    max_calls: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)


ModelExposureSource = Annotated[
    ModelInMemorySource | ModelGithubSource, Field(discriminator="backend")
]


class ModelExposureProbeOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    settings: ModelExposureSettings
    source: ModelExposureSource


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelExposureProbeOverlay:
    return load_structured_overlay(NODE_NAME, ModelExposureProbeOverlay, environ)


class ModelExposurePublish(BaseModel):
    """How a reading row is published: its lane and actor, how long it stays fresh, its fault."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    #: How long a published reading stays fresh; older, a reader treats it as NOT WORKING.
    publish_max_age_seconds: int = Field(ge=1)
    injected_fault: str = Field(min_length=1)


class ModelExposureHost(BaseModel):
    """A host that runs the hourly schedule: its scheduler, its venv and its unit environment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    scheduler: Literal["systemd-user"]
    #: The venv whose interpreter runs the installed unit, relative to the registry root (or
    #: absolute): a dedicated venv of an installed unit, never a dispatch venv or a worktree.
    venv: str = Field(min_length=1)
    #: An environment file the unit reads when it exists (``EnvironmentFile=-``). The token the
    #: GitHub source names in ``token_env`` is supplied there, never in the overlay or the unit.
    environment_file: str = Field(min_length=1)
    #: Environment the host's installed unit carries (the ledger route and its roots).
    environment: dict[str, str]


class ModelExposureReadingOverlay(ModelExposureProbeOverlay):
    """The probe's overlay plus what the reading writer and the schedule need."""

    ledger_append: ModelLedgerAppendRoute
    publish: ModelExposurePublish
    hosts: tuple[ModelExposureHost, ...]

    @model_validator(mode="after")
    def _shape(self) -> ModelExposureReadingOverlay:
        names = [host.name for host in self.hosts]
        if len(names) != len(set(names)):
            raise ValueError("a host is declared twice")
        return self

    def host(self, name: str) -> ModelExposureHost | None:
        return next((host for host in self.hosts if host.name == name), None)

    def overlay_hash(self) -> str:
        """Hash of what the reading depends on, so a settings change shows in the row."""
        text = json.dumps(
            {
                "settings": self.settings.model_dump(mode="json"),
                "source": self.source.model_dump(mode="json", exclude={"fixture"}),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]


def load_reading_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelExposureReadingOverlay:
    return load_structured_overlay(NODE_NAME, ModelExposureReadingOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelExposureHost",
    "ModelExposureProbeOverlay",
    "ModelExposurePublish",
    "ModelExposureReadingOverlay",
    "ModelExposureSource",
    "ModelGithubSource",
    "ModelInMemoryFixture",
    "ModelInMemorySource",
    "ModelTokenMint",
    "load_overlay",
    "load_reading_overlay",
]

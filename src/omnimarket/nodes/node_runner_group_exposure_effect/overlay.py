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
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
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


class ModelMintSecretStore(BaseModel):
    """The secret store the App id and key are read from, and the folder that holds both."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The store block: addressing and the NAMES of the machine identity's credentials, never a value.
    secret_store: ModelSecretStoreOverlay
    #: The store folder holding the App id and the App private key.
    folder: str = Field(pattern=r"^/")


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
    #: Where the two secrets are read from. Null keeps the process environment (or the lane's store);
    #: a block reads them through that store with the machine's identity file and nothing else.
    store: ModelMintSecretStore | None


class ModelWriteNodeGroupRead(BaseModel):
    """Read the runner group through the GitHub write node over the bus, never with the source's own token.

    The write node's App already holds the organization runner permission; its overlay names the
    organization and group id it serves. Every field is a setting, never a credential.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The bus lane the write node answers on (the lab dev lane).
    bus_lane: str = Field(min_length=1)
    #: The NAME of the environment variable holding the workspace root the bus opener needs.
    workspace_env: str = Field(min_length=1)
    #: A repository of the organization: the write node reads the organization from its owner part.
    anchor_repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    #: The lane name the request carries.
    requested_by_lane: str = Field(min_length=1)
    #: Seconds to wait for the write node's terminal event.
    timeout_seconds: float = Field(gt=0)


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
    #: When set, the runner group's own settings and repositories are read through the write node
    #: over the bus (the organization read the source's own token may not hold); null keeps the
    #: direct read. The workflow and run reads still use the source's token.
    group_read: ModelWriteNodeGroupRead | None
    #: The NAME of the App permission the organization read of the group needs, named in a
    #: refusal; never a value.
    org_read_permission: str = Field(min_length=1)
    #: Calls one read may make; the call past it is not made.
    max_calls: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _group_read_names_the_org(self) -> ModelGithubSource:
        route = self.group_read
        if route is not None:
            owner = route.anchor_repository.split("/", 1)[0]
            if owner.lower() != self.org.lower():
                raise ValueError(
                    "group_read.anchor_repository must belong to the source's org"
                )
        return self


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
    "ModelMintSecretStore",
    "ModelTokenMint",
    "ModelWriteNodeGroupRead",
    "load_overlay",
    "load_reading_overlay",
]

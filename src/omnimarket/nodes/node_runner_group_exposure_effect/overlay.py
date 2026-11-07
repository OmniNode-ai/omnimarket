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

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_runner_group_exposure_compute.models.model_runner_group_exposure import (
    EnumExposureReadFailure,
    ModelExposureSettings,
    ModelGroupJob,
    ModelGroupWorkflowFile,
    ModelRunnerGroupFacts,
)

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
    token_env: str = Field(min_length=1)
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


__all__ = [
    "NODE_NAME",
    "ModelExposureProbeOverlay",
    "ModelExposureSource",
    "ModelGithubSource",
    "ModelInMemoryFixture",
    "ModelInMemorySource",
    "load_overlay",
]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_bus_login_exposure_effect: every setting of the bus login probe.

Found at ``<root>/node_bus_login_exposure_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships two examples with synthetic values: an in-memory
source (the default way to run it) and a Redpanda source. Nothing here has a default: an absent
key refuses the load and names it. Credentials are never stored: ``username_env`` and
``password_env`` name the environment variables that hold them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_bus_login_exposure_effect.models.model_bus_login_exposure import (
    EnumBusReadFailure,
)

NODE_NAME = "node_bus_login_exposure_effect"


class ModelExposureSettings(BaseModel):
    """What the login is allowed to see; anything else it sees is an exposure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_topics: tuple[str, ...]
    allowed_topic_prefixes: tuple[str, ...]
    allowed_groups: tuple[str, ...]
    allowed_group_prefixes: tuple[str, ...]
    #: How old a read may be before it reads UNKNOWN.
    max_age_seconds: int = Field(ge=1)


class ModelInMemoryFixture(BaseModel):
    """The view one in-memory read returns (or the failure it ends in)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topics: tuple[str, ...]
    groups: tuple[str, ...]
    #: How many seconds before the request's ``now`` the fixture was read.
    read_age_seconds: int = Field(ge=0)
    read_failure: EnumBusReadFailure | None = None


class ModelInMemorySource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["inmemory"]
    fixture: ModelInMemoryFixture


class ModelRedpandaSource(BaseModel):
    """The Redpanda source: the broker's metadata and group listing, as the login sees them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["redpanda"]
    bootstrap_servers: str = Field(min_length=1)
    security_protocol: str = Field(min_length=1)
    sasl_mechanism: str = Field(min_length=1)
    #: The NAMES of the environment variables holding the login; the values are never stored.
    username_env: str = Field(min_length=1)
    password_env: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)


ModelBusSource = Annotated[
    ModelInMemorySource | ModelRedpandaSource, Field(discriminator="backend")
]


class ModelBusLoginExposureOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    settings: ModelExposureSettings
    source: ModelBusSource


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelBusLoginExposureOverlay:
    return load_structured_overlay(NODE_NAME, ModelBusLoginExposureOverlay, environ)


__all__ = [
    "NODE_NAME",
    "ModelBusLoginExposureOverlay",
    "ModelBusSource",
    "ModelExposureSettings",
    "ModelInMemoryFixture",
    "ModelInMemorySource",
    "ModelRedpandaSource",
    "load_overlay",
]

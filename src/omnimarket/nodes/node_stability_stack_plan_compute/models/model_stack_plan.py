# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models for the stability stack plan compute node (OMN-23968).

Every model is frozen and forbids extra keys; the overlay models carry no
defaults, so an absent key refuses the overlay load. The overlay models (the
stack descriptions and the reserved lanes of the other stacks) and the
request/response models share this module; ``overlay.py`` only loads.

Rows name things symbolically. A stack description names compose variables,
environment variables and container names as data; no host, port, tag or path
is written in code. One description refuses itself when a health read names a
port key the stack does not publish, a published port or a container name is
repeated inside the stack, or one service is named in two tiers, so a contract
carrying such a row fails to load.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_STACK_NAME = r"^[a-z0-9-]+$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Overlay models (loaded from <root>/node_stability_stack_plan_compute/overlay.yaml)
# ---------------------------------------------------------------------------


class ModelReservedLane(_Frozen):
    """What another stack already holds: its names and published ports, as data."""

    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    compose_project: str = Field(min_length=1)
    container_names: tuple[str, ...]
    volumes: tuple[str, ...]
    ports: tuple[int, ...]


class ModelStackTier(_Frozen):
    """One bring-up tier: the services brought up together, in order."""

    name: str = Field(min_length=1)
    services: tuple[str, ...] = Field(min_length=1)


class ModelHealthRead(_Frozen):
    """One read-only health probe against a container of the stack."""

    container: str = Field(min_length=1)
    #: A key of the stack's ``ports`` mapping, so no port number is duplicated here.
    port_key: str = Field(min_length=1)
    path: str = Field(min_length=1)
    expect_status: int


class ModelEventRoundtrip(_Frozen):
    """One publish on the stack's broker the plan reads back to prove the bus."""

    broker_container: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    message: str


class ModelStackDescription(_Frozen):
    """One declared stack: how it renders, what it holds and how it is verified.

    ``name_rewrites`` are text pairs applied to each compose file when it is
    rendered; ``container_names`` and ``volumes`` are the names AFTER rewrite.
    ``ports`` maps a compose variable name to the published host port.
    """

    name: str = Field(pattern=_STACK_NAME)
    host: str = Field(min_length=1)
    compose_project: str = Field(min_length=1)
    #: The name of the environment variable that names the checkout holding the
    #: compose files; never a path itself.
    source_dir_env: str = Field(min_length=1)
    compose_files: tuple[str, ...] = Field(min_length=1)
    name_rewrites: tuple[tuple[str, str], ...]
    #: Compose variables, including the image tags.
    env: tuple[tuple[str, str], ...]
    #: Compose variable name -> published host port.
    ports: tuple[tuple[str, int], ...]
    container_names: tuple[str, ...]
    volumes: tuple[str, ...]
    release_tag: str = Field(min_length=1)
    #: Bring-up order; the plan raises one ``up`` step per tier, in this order.
    tiers: tuple[ModelStackTier, ...] = Field(min_length=1)
    health_reads: tuple[ModelHealthRead, ...]
    event_roundtrip: ModelEventRoundtrip
    remove_volumes_on_down: bool

    @model_validator(mode="after")
    def _shape(self) -> Self:
        port_keys = [key for key, _port in self.ports]
        if len(port_keys) != len(set(port_keys)):
            raise ValueError("a compose port variable is named twice")
        published = [port for _key, port in self.ports]
        if len(published) != len(set(published)):
            raise ValueError("a published port number is used twice in one stack")
        if len(self.container_names) != len(set(self.container_names)):
            raise ValueError("a container name is used twice in one stack")
        known_keys = set(port_keys)
        for read in self.health_reads:
            if read.port_key not in known_keys:
                raise ValueError(
                    f"health read of {read.container} names port_key "
                    f"{read.port_key!r} the stack does not publish"
                )
        seen_services: set[str] = set()
        for tier in self.tiers:
            duplicated = seen_services.intersection(tier.services)
            if duplicated:
                raise ValueError(
                    "a service is named in two tiers: " + ", ".join(sorted(duplicated))
                )
            seen_services.update(tier.services)
        return self


class ModelStackPlanOverlay(_Frozen):
    """The whole overlay: the declared stacks and the lanes reserved by others.

    ``tag_pattern`` is the regex a stack's ``release_tag`` must fullmatch; the
    pattern admits only dev-merge tags.
    """

    tag_pattern: str = Field(min_length=1)
    stacks: tuple[ModelStackDescription, ...] = Field(min_length=1)
    reserved: tuple[ModelReservedLane, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_stack_names(self) -> Self:
        names = [stack.name for stack in self.stacks]
        if len(names) != len(set(names)):
            raise ValueError("a stack name is declared twice")
        return self


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ModelStackPlanRequest(_Frozen):
    """Which declared stack to plan, and whether to install or remove it."""

    stack: str = Field(min_length=1)
    action: Literal["install", "remove"]


class EnumStackStepKind(StrEnum):
    """One kind of step an install or remove plan carries."""

    RENDER = "render"
    SNAPSHOT = "snapshot"
    UP = "up"
    READ_SERVICES = "read_services"
    READ_HEALTH = "read_health"
    EVENT_ROUNDTRIP = "event_roundtrip"
    DOWN = "down"
    READ_GONE = "read_gone"


class ModelStackStep(_Frozen):
    """One step of the plan.

    ``target`` is a service list joined by a single space (an ``up`` step), a
    container (a ``read_health`` step or the broker of an ``event_roundtrip``)
    or a compose project (a ``down`` or ``read_gone`` step); ``detail`` names
    the path, expected status, topic or removal scope the step verifies.
    """

    index: int
    kind: EnumStackStepKind
    tier: str = ""
    target: str = ""
    detail: str = ""


class EnumStackRefusalKind(StrEnum):
    """Why the plan refused. Any refusal means no steps at all."""

    UNKNOWN_STACK = "unknown_stack"
    BAD_RELEASE_TAG = "bad_release_tag"
    PROJECT_COLLISION = "project_collision"
    CONTAINER_COLLISION = "container_collision"
    VOLUME_COLLISION = "volume_collision"
    PORT_COLLISION = "port_collision"


class ModelStackRefusal(_Frozen):
    """One refusal: what is wrong (``subject``) and what it collides with."""

    kind: EnumStackRefusalKind
    subject: str = Field(min_length=1)
    #: Empty only for ``unknown_stack``, where there is nothing to collide with.
    against: str


class ModelStackPlan(_Frozen):
    """The plan: ordered steps and their rollback, or the refusals instead.

    ``render_digest`` is the sha256 hex of the canonical sorted-key JSON of the
    stack description; empty when the request named an unknown stack.
    """

    status: Literal["planned", "refused"]
    stack: str = Field(min_length=1)
    host: str
    compose_project: str
    action: Literal["install", "remove"]
    steps: tuple[ModelStackStep, ...]
    rollback_steps: tuple[ModelStackStep, ...]
    refusals: tuple[ModelStackRefusal, ...]
    render_digest: str


__all__ = [
    "EnumStackRefusalKind",
    "EnumStackStepKind",
    "ModelEventRoundtrip",
    "ModelHealthRead",
    "ModelReservedLane",
    "ModelStackDescription",
    "ModelStackPlan",
    "ModelStackPlanOverlay",
    "ModelStackPlanRequest",
    "ModelStackRefusal",
    "ModelStackStep",
    "ModelStackTier",
]

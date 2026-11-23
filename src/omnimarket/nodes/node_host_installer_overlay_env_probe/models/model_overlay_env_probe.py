# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the overlay environment probe.

A request names hosts, the nodes assigned to each and the environment NAMES that are set on each host. A value never
enters a request or a result: ``set_names`` carries names only, and a row carries a state, never a value.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_lab_placement_compute.models.model_lab_placement import (
    ModelHostTableVerdict,
)


class ModelProbeHost(BaseModel):
    """One host: the nodes assigned to it and the names that read as set there (names only, never values)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str = Field(min_length=1)
    nodes: tuple[str, ...]
    set_names: tuple[str, ...] = ()


class ModelProbePlacement(BaseModel):
    """The lane placement the probe applies: the node being placed and the verdicts placement already gave per host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(min_length=1)
    verdicts: tuple[ModelHostTableVerdict, ...]


class ModelOverlayEnvProbeRequest(BaseModel):
    """``overlay_roots`` are searched in order for ``<root>/<node>/overlay.yaml``, as ``ONEX_SKILL_OVERLAY_ROOTS`` is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_roots: tuple[str, ...]
    hosts: tuple[ModelProbeHost, ...]
    placement: ModelProbePlacement | None = None


class ModelEnvNameRow(BaseModel):
    """One name a node declares, as set or unset on one host. ``declared_by`` is the key path in ``source``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    node: str
    name: str
    source: Literal["overlay", "contract"]
    declared_by: str
    state: Literal["set", "unset"]


class ModelEnvAlarm(BaseModel):
    """The one alarm for a host, a node and an unset name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    node: str
    name: str
    message: str


class ModelNodeUnavailable(BaseModel):
    """A host that is unavailable for a node because these names are unset there."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    node: str
    unset_names: tuple[str, ...]


class ModelOverlayEnvProbeResult(BaseModel):
    """``rows`` one per host, node and name; ``alarms`` one per unset row; ``verdicts`` and ``eligible_hosts`` are the
    placement for the request's node after the probe (empty when the request carried no placement)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "refused"]
    exit_code: int = 0
    reason: str = ""
    rows: tuple[ModelEnvNameRow, ...] = ()
    alarms: tuple[ModelEnvAlarm, ...] = ()
    unavailable: tuple[ModelNodeUnavailable, ...] = ()
    verdicts: tuple[ModelHostTableVerdict, ...] = ()
    eligible_hosts: tuple[str, ...] = ()

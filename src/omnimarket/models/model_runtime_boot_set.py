# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed declaration, observation and verdict for a deployment's declared boot set (one-runtime plan, T28).

A runtime that loads every installed contract and subscribes every topic they declare does that work at each start,
whether or not a message ever arrives. A deployment instead declares the set it loads. This module holds the shapes
for judging one boot against that declaration; no count and no contract name is written in a handler, because the
declaration, the catalog and the observation are all data the caller passes in.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

BootSetStatus = Literal["PASS", "FAIL", "UNKNOWN"]
BootSetRule = Literal[
    "NO_DECLARED_SET",
    "DECLARATION_FOR_OTHER_DEPLOYMENT",
    "CONTRACT_NOT_ANNOUNCED",
    "LOADED_OUTSIDE_DECLARED_SET",
    "SUBSCRIBED_OUTSIDE_DECLARED_SET",
    "DECLARED_NOT_LOADED",
    "BOOT_NOT_READ",
]


class ModelBootCatalogEntry(BaseModel):
    """One announced contract and the topics it declares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: str = Field(min_length=1)
    subscribe_topics: tuple[str, ...] = ()
    publish_topics: tuple[str, ...] = ()


class ModelBootSetDeclaration(BaseModel):
    """The contracts one deployment loads at start, as its overlay lists them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    deployment: str = Field(min_length=1)
    contracts: tuple[str, ...]
    # Topics the runtime kernel itself subscribes whatever the contracts say (its own consumers); data, not a handler constant.
    kernel_topics: tuple[str, ...] = ()


class ModelBootObservation(BaseModel):
    """What one boot actually did, read from the running runtime's log.

    ``time_to_healthy_seconds`` and ``memory_mib`` are measured facts carried through to the verdict; they are not
    judged here, because a bound on them is a deployment setting and not this node's.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    attached_contracts: tuple[str, ...] = ()
    subscribed_topics: tuple[str, ...] = ()
    read_error: str = ""
    time_to_healthy_seconds: float | None = Field(default=None, ge=0)
    memory_mib: float | None = Field(default=None, ge=0)


class ModelRuntimeBootSetRequest(BaseModel):
    """A deployment, its declared boot set (None when it declares none), the catalog and one boot's observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    deployment: str = Field(min_length=1)
    declaration: ModelBootSetDeclaration | None = None
    catalog: tuple[ModelBootCatalogEntry, ...]
    observation: ModelBootObservation


class ModelRuntimeBootSetVerdict(BaseModel):
    """FAIL when the boot did work outside the declared set or has no declared set, UNKNOWN when it was not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    deployment: str
    status: BootSetStatus
    rules: tuple[BootSetRule, ...]
    declared_contract_count: int
    loaded_contract_count: int
    contracts_over_declared: int
    declared_topic_count: int
    subscribed_topic_count: int
    topics_over_declared: int
    excess_contracts: tuple[str, ...]
    excess_topics: tuple[str, ...]
    missing_contracts: tuple[str, ...]
    unannounced_contracts: tuple[str, ...]
    # The value the deployment overlay hands the runtime as ONEX_RUNTIME_NODE_SELECTION; empty when nothing is declared.
    selection_env: str
    time_to_healthy_seconds: float | None = None
    memory_mib: float | None = None
    detail: str


__all__ = [
    "BootSetRule",
    "BootSetStatus",
    "ModelBootCatalogEntry",
    "ModelBootObservation",
    "ModelBootSetDeclaration",
    "ModelRuntimeBootSetRequest",
    "ModelRuntimeBootSetVerdict",
]

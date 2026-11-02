# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the landing worker's engine choice (OMN-17427).

Frozen Pydantic models reject undeclared fields.
"""

from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.stop_gate import ModelStopGateFacts
from omnimarket.nodes.node_landing_worker_engine_compute.models.model_worker_engine_facts import (
    ModelEngineAvailability,
    ModelWorkerEngineConfig,
)


class ModelLandingWorkerEngineRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # the node's engine for this lease (brief.engine), a Claude rung; with ``availability`` it is the id
    # the caller's availability carries
    requested_engine: str
    # landing_facts.codex_first: a code or mechanical class on the first rung
    codex_first: bool
    # a shared_cause or cause_diagnosis brief: its prompt has no Codex-engine form
    cause: bool
    # Typed facts (OMN-21958): each engine's availability, recounted under the placement lock, and the node's
    # config (the Codex engine id and the two Codex routes) from its contract and overlay.
    availability: tuple[ModelEngineAvailability, ...] | None = None
    config: ModelWorkerEngineConfig | None = None
    # Engines the caller disallows for any worker, by the ids the availability carries (OMN-21958).
    # Empty: the choice is as before.
    disallowed_engines: tuple[str, ...] = ()
    # The open andon stops, the ``andon_stop`` overlay policy and now, supplied by the caller. A stop in an
    # enforce class on an engine (by the engine as named or by a ladder rung it runs) keeps that engine off
    # every route, the Codex fallback included. None: no gate is installed.
    stop_gate: ModelStopGateFacts | None = None
    # Deprecated: the pre-facts inputs, read only when ``availability`` is None, for a caller not yet moved.
    # landing_placement.Reading per pool host, recounted under the placement lock
    readings: tuple[Any, ...] = ()
    # codex_placement.codex_hosts: the hosts the table marks codex: true
    codex_allowed: frozenset[str] = frozenset()
    # the switches LANDING_CODEX_MECHANICAL and LANDING_CODEX_FALLBACK
    env: Mapping[str, str] = Field(default_factory=dict)
    # the landing_placement module: choose, choose_codex, preflight_skips, resolve_engine, CODEX_ENGINE
    placement: Any = None

    @model_validator(mode="after")
    def one_source_of_engine_facts(self) -> Self:
        if self.availability is None:
            if self.placement is None:
                raise ValueError(
                    "engine facts are missing: supply availability with config"
                )
            if self.config is not None:
                raise ValueError("config goes with availability, not with placement")
            return self
        if self.placement is not None:
            raise ValueError("supply availability or placement, not both")
        if self.config is None:
            raise ValueError("availability needs config")
        return self


class ModelLandingWorkerEngineResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # landing_placement.Placement, or None when no host can take the worker now
    pick: Any
    # the placement record's routing fields
    record: dict[str, Any] = Field(default_factory=dict)

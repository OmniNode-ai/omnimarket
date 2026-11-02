# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the landing worker's engine choice (OMN-17427).

Frozen Pydantic models reject undeclared fields.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.stop_gate import ModelStopGateFacts


class ModelLandingWorkerEngineRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # landing_placement.Reading per pool host, recounted under the placement lock
    readings: tuple[Any, ...]
    # the node's engine for this lease (brief.engine), a Claude rung
    requested_engine: str
    # landing_facts.codex_first: a code or mechanical class on the first rung
    codex_first: bool
    # a shared_cause or cause_diagnosis brief: its prompt has no Codex-engine form
    cause: bool
    # codex_placement.codex_hosts: the hosts the table marks codex: true
    codex_allowed: frozenset[str]
    # the switches LANDING_CODEX_MECHANICAL and LANDING_CODEX_FALLBACK
    env: Mapping[str, str]
    # the landing_placement module: choose, choose_codex, preflight_skips, resolve_engine, CODEX_ENGINE
    placement: Any
    # Engines the caller disallows for any worker, by the ids the placement module returns (OMN-21958).
    # Empty: the choice is as before.
    disallowed_engines: tuple[str, ...] = ()
    # The open andon stops, the ``andon_stop`` overlay policy and now, supplied by the caller. A stop in an
    # enforce class on an engine (by the engine as named or by a ladder rung it runs) keeps that engine off
    # every route, the Codex fallback included. None: no gate is installed.
    stop_gate: ModelStopGateFacts | None = None


class ModelLandingWorkerEngineResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # landing_placement.Placement, or None when no host can take the worker now
    pick: Any
    # the placement record's routing fields
    record: dict[str, Any] = Field(default_factory=dict)

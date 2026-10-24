# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the landing worker's engine choice (OMN-17427).

Frozen Pydantic models reject undeclared fields.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class ModelLandingWorkerEngineResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # landing_placement.Placement, or None when no host can take the worker now
    pick: Any
    # the placement record's routing fields
    record: dict[str, Any] = Field(default_factory=dict)

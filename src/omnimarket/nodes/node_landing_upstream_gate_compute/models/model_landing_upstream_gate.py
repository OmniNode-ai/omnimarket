# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the upstream_open gate of one PR head (OMN-19998).

Frozen Pydantic models reject undeclared fields.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelLandingUpstreamGateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # ``repo#n`` of the gated PR
    pr: str
    # the PR's live head sha
    head: str
    # the sidecar's ``external_judged`` of the PR: ref -> head, kind, at
    judged: Mapping[str, Mapping[str, str]]
    # the PR watcher's records, ``repo#n``
    records: Mapping[str, Mapping[str, Any]] = Field(default_factory=dict)
    # the tick's stamp, ``YYYY-MM-DDTHH:MM:SSZ``; with ``hours`` bounds a kind that has a backstop
    now: str = ""
    # the backstop of every known kind but upstream_open
    hours: float = 1.0


class ModelLandingUpstreamGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # every judged ref that still stands at this head, by ref
    standing: tuple[str, ...]
    # the standing refs of a kind with no backstop (upstream_open), by ref
    unbounded: tuple[str, ...]
    # the ref the worker gate holds the PR on this tick, or ""
    gate_ref: str

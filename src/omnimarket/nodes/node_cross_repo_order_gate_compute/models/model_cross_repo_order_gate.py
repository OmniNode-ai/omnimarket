# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the cross-repository order gate (OMN-23345).

Frozen Pydantic models reject undeclared fields.
"""

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumCrossRepoOrderVerdict(StrEnum):
    ALLOW = "allow"
    HOLD = "hold"


class EnumCrossRepoOrderReason(StrEnum):
    """Why the gate said what it said; the first four hold, the last two allow."""

    PREDECESSOR_OPEN = "predecessor_open"
    PREDECESSOR_CLOSED = "predecessor_closed"
    PREDECESSOR_UNREAD = "predecessor_unread"
    DECLARATION_UNPARSEABLE = "declaration_unparseable"
    PREDECESSORS_MERGED = "predecessors_merged"
    NO_DECLARED_ORDER = "no_declared_order"


class ModelCrossRepoOrderGateRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    # ``repo#n`` of the PR the controller is about to land
    pr: str
    # PR bodies by ``repo#n``: the gated PR's own, and every sibling that may declare ``Lands-Before``
    bodies: Mapping[str, str] = Field(default_factory=dict)
    # the PR watcher's state of each ref, ``repo#n`` -> OPEN | MERGED | CLOSED; a ref not listed is unread
    states: Mapping[str, str] = Field(default_factory=dict)


class ModelCrossRepoOrderGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    verdict: EnumCrossRepoOrderVerdict
    reason: EnumCrossRepoOrderReason
    # every change this PR is declared to land after, by ref
    predecessors: tuple[str, ...] = ()
    # the predecessors not merged, by ref: the owning changes the controller names while it holds
    waiting_on: tuple[str, ...] = ()
    # declaration text that named no ``repo#n``
    unparseable: tuple[str, ...] = ()

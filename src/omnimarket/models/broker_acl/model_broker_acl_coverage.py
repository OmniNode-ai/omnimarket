# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The coverage check: every topic a node contract declares is covered by a declared grant or a frozen baseline entry.

A topic use is one (node, topic, direction) the contract's event_bus declares. It is covered when the declared rows carry
every operation the direction needs (the operations are the render contract's config). A node with uncovered uses
is held at a baseline of how many it had when the baseline was frozen: more is a new uncovered topic and refuses, fewer
means the baseline is stale and refuses until it is lowered, so the baseline only shrinks (OMN-24005).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.broker_acl.model_broker_acl_derive import ModelContractTopicUse
from omnimarket.models.broker_acl.model_broker_acl_overlay import (
    ModelBrokerAclFinding,
    ModelBrokerAclRow,
)


class ModelCoverageBaselineEntry(BaseModel):
    """One node held at the number of uncovered topic uses it had when the baseline was frozen."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    node: str = Field(min_length=1)
    uncovered_uses: int = Field(ge=1)


class ModelBrokerAclCoverageRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    topic_uses: tuple[ModelContractTopicUse, ...]
    declared_rows: tuple[ModelBrokerAclRow, ...] = ()
    baseline: tuple[ModelCoverageBaselineEntry, ...] = ()


class ModelBrokerAclCoverageResult(BaseModel):
    """Refused names every uncovered topic use not held by the baseline, and every stale baseline entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["covered", "refused"]
    nodes: int = Field(default=0, ge=0)
    uncovered_uses: int = Field(default=0, ge=0)
    baselined_uses: int = Field(default=0, ge=0)
    findings: tuple[ModelBrokerAclFinding, ...] = ()

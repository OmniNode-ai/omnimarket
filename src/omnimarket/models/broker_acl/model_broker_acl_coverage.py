# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The coverage check: every topic use a change adds to a node contract is covered by a declared grant (OMN-24005).

A topic use is one (node, topic, direction) the contract's event_bus declares. It is covered when the declared rows carry every
operation the direction needs (the operations are the render contract's config). Only uses the change adds are judged: a use
the base branch's contracts already declare is the debt that predates the check, and there is no list of exceptions to maintain.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.broker_acl.model_broker_acl_derive import ModelContractTopicUse
from omnimarket.models.broker_acl.model_broker_acl_overlay import (
    ModelBrokerAclFinding,
    ModelBrokerAclRow,
)


class ModelBrokerAclCoverageRequest(BaseModel):
    """topic_uses are the contracts as they stand, base_topic_uses the contracts of the base branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    topic_uses: tuple[ModelContractTopicUse, ...]
    base_topic_uses: tuple[ModelContractTopicUse, ...]
    declared_rows: tuple[ModelBrokerAclRow, ...] = ()


class ModelBrokerAclCoverageResult(BaseModel):
    """Refused names every added topic use no declared grant covers."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["covered", "refused"]
    new_uses: int = Field(default=0, ge=0)
    uncovered_new_uses: int = Field(default=0, ge=0)
    findings: tuple[ModelBrokerAclFinding, ...] = ()

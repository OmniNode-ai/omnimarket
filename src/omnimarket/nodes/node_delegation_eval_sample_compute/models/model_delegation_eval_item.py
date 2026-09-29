# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Selected identity and its sampling provenance."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_eval_sample_compute.models.enum_delegation_eval_source import (
    EnumDelegationEvalSource,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_key import (
    ModelDelegationEvalKey,
)


class ModelDelegationEvalItem(BaseModel):
    """Selected identity and its sampling provenance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: ModelDelegationEvalKey
    task_class: str
    stratum: str
    source: EnumDelegationEvalSource

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied sampling window, query, seed and candidate identities."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_candidate import (
    ModelDelegationEvalCandidate,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_key import (
    ModelDelegationEvalKey,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_sampling_config import (
    ModelDelegationEvalSamplingConfig,
)


class ModelDelegationEvalSampleRequest(BaseModel):
    """Caller-supplied sampling window, query, seed and candidate identities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: str
    window_start: str
    window_end: str
    query_text: str
    house_tenant_id: str
    sampling: ModelDelegationEvalSamplingConfig
    candidates: tuple[ModelDelegationEvalCandidate, ...]
    imported_keys: tuple[ModelDelegationEvalKey, ...] = ()

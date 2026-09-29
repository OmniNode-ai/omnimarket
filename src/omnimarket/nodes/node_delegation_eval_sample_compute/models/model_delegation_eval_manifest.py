# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic content-free sample manifest and exclusion accounting."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_item import (
    ModelDelegationEvalItem,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_quota import (
    ModelDelegationEvalQuota,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_rejected_import import (
    ModelDelegationEvalRejectedImport,
)
from omnimarket.nodes.node_delegation_eval_sample_compute.models.model_delegation_eval_shortfall import (
    ModelDelegationEvalShortfall,
)


class ModelDelegationEvalManifest(BaseModel):
    """Deterministic content-free sample manifest and exclusion accounting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_id: str
    seed: str
    window_start: str
    window_end: str
    query_text: str
    quotas: tuple[ModelDelegationEvalQuota, ...]
    items: tuple[ModelDelegationEvalItem, ...]
    shortfalls: tuple[ModelDelegationEvalShortfall, ...]
    excluded_holdout_bucket: int
    excluded_customer_tenant: int
    rejected_imports: tuple[ModelDelegationEvalRejectedImport, ...]

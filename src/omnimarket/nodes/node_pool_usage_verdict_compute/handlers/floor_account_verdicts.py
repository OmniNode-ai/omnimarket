# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's accounts judged by the pool usage verdict, as one typed verdict fact per account (OMN-23609).

A seam for the factory floor's Limits panel: the floor's saved rate rows and token pool mirrors go through the
verdict handler, and each account's verdict comes out as the floor's own fact model, so the caller needs no model of this node. With no thresholds (no overlay) nothing is judged and the answer is None: the panel then says UNKNOWN and
never draws an account usable on a threshold spelled in code.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import NAMESPACE_OID, uuid5

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_factory_floor_input import (
    ModelFloorCloudAccount,
    ModelLimitAccountVerdict,
    ModelLimitTokenPool,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.handlers.handler_pool_usage_verdict import (
    HandlerPoolUsageVerdict,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.handlers.mapper_floor_readings import (
    readings_from_floor,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.models import (
    ModelPoolUsagePolicy,
    ModelPoolUsageRequest,
)
from omnimarket.nodes.node_pool_usage_verdict_compute.overlay import load_policy

_CORRELATION = uuid5(NAMESPACE_OID, "node_pool_usage_verdict_compute:floor-limits")


def policy_or_none() -> ModelPoolUsagePolicy | None:
    """The deployed thresholds, or None when no overlay is installed (a refused load is never a default)."""
    try:
        return load_policy()
    except NodeOverlayError:
        return None


def account_verdicts_from_floor(
    accounts: Sequence[ModelFloorCloudAccount] | None,
    pools: Sequence[ModelLimitTokenPool] | None,
    now: datetime,
    policy: ModelPoolUsagePolicy | None,
) -> tuple[ModelLimitAccountVerdict, ...] | None:
    """One verdict fact per account; None when the accounts, the pools or the thresholds were not read."""
    if accounts is None or pools is None or policy is None:
        return None
    result = HandlerPoolUsageVerdict().handle(
        ModelPoolUsageRequest(
            now=now,
            policy=policy,
            readings=readings_from_floor(accounts, pools),
            correlation_id=_CORRELATION,
        )
    )
    return tuple(
        ModelLimitAccountVerdict(label=u.label, verdict=u.verdict)
        for u in result.labels
    )


__all__ = ["account_verdicts_from_floor", "policy_or_none"]

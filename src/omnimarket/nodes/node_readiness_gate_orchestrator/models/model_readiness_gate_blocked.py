# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A readiness result whose state is BLOCKED.

It adds no field, so its wire shape is exactly ``ModelDeploymentReadinessResult``.
The runtime picks a returned model's topic from its class name through the
contract's ``published_events``; ``ModelDeploymentReadinessResult`` carries no
verdict field the runtime reads, so only a distinct class lets a BLOCKED
decision reach ``readiness-gate-blocked``.
"""

from __future__ import annotations

from omnibase_compat.contracts.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)


class ModelReadinessGateBlocked(ModelDeploymentReadinessResult):
    """Published on the blocked terminal topic."""


__all__: list[str] = ["ModelReadinessGateBlocked"]

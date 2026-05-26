# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerEnvSyncAlertEffect — STUB.

ONEX node type: EFFECT_GENERIC — side-effecting, writes to Linear and emits friction events.
Ticket: OMN-12227
"""

from __future__ import annotations

from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_request import (
    ModelEnvSyncAlertRequest,
)
from omnimarket.nodes.node_env_sync_alert_effect.models.model_env_sync_alert_result import (
    ModelEnvSyncAlertResult,
)


class HandlerEnvSyncAlertEffect:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelEnvSyncAlertRequest) -> ModelEnvSyncAlertResult:
        raise NotImplementedError(  # stub-ok
            "node_env_sync_alert_effect is not yet implemented (OMN-12227). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local alert transition compute: environment rows around a write in, transitions out.

The derivation and the fold plan are shared (``omnimarket.handlers.handler_local_alert_fold``)
so the environment writer that publishes transitions and the local_alerts projection
that folds them use the same decision without importing this node.
"""

from __future__ import annotations

from omnimarket.handlers.handler_local_alert_fold import (
    LOCAL_ALERTS_KEY,
    LOCAL_ALERTS_TABLE,
    derive_local_alert_transitions,
    plan_local_alert_fold,
)
from omnimarket.nodes.node_local_alert_transition_compute.models import (
    ModelLocalAlertTransitionRequest,
    ModelLocalAlertTransitionResult,
)


class HandlerLocalAlertTransition:
    def handle(
        self, request: ModelLocalAlertTransitionRequest
    ) -> ModelLocalAlertTransitionResult:
        return ModelLocalAlertTransitionResult(
            transitions=derive_local_alert_transitions(request.before, request.after)
        )


__all__ = [
    "LOCAL_ALERTS_KEY",
    "LOCAL_ALERTS_TABLE",
    "HandlerLocalAlertTransition",
    "derive_local_alert_transitions",
    "plan_local_alert_fold",
]

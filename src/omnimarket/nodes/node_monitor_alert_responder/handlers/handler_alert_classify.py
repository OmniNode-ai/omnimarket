# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Alert classification handler for node_monitor_alert_responder.

Classifies inbound monitor alerts into three tiers:
  RECOVERABLE  — known pattern with a runbook; dispatch fix agent
  UNKNOWN      — unrecognised pattern; emit alert-unhandled event
  ESCALATE     — CRITICAL severity or unhandled + restart storm; P0 response
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from omnimarket.nodes.node_monitor_alert_responder.models.model_alert_event import (
    ModelAlertEvent,
)
from omnimarket.nodes.node_monitor_alert_responder.models.model_alert_response import (
    AlertTier,
    ModelAlertResponse,
)

logger = logging.getLogger(__name__)

# Patterns whose severity alone warrants immediate escalation.
_ESCALATE_SEVERITIES: frozenset[str] = frozenset({"CRITICAL"})

# Patterns considered auto-recoverable (keyword substring match on pattern_matched).
_RECOVERABLE_PATTERNS: tuple[str, ...] = (
    "oom",
    "out_of_memory",
    "memory",
    "restart",
    "crash_loop",
    "connection_refused",
    "timeout",
    "disk_full",
    "disk_pressure",
)

# Restart count at or above which UNKNOWN is promoted to ESCALATE.
_ESCALATE_RESTART_THRESHOLD = 5


class HandlerAlertClassify:
    """Classifies monitor alerts and returns a tier + routing decision."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    async def handle(
        self,
        correlation_id: UUID,
        input_data: ModelAlertEvent,
    ) -> ModelAlertResponse:
        """Classify the alert and produce a routing decision.

        Tier logic:
          1. CRITICAL severity → ESCALATE immediately.
          2. Pattern matches a known recoverable keyword → RECOVERABLE.
          3. Restart count >= threshold → ESCALATE (restart storm).
          4. Anything else → UNKNOWN.
        """
        logger.info(
            "AlertClassify: correlation_id=%s alert_id=%s severity=%s pattern=%s",
            correlation_id,
            input_data.alert_id,
            input_data.severity,
            input_data.pattern_matched,
        )

        tier, notes = self._classify(input_data)

        return ModelAlertResponse(
            alert_id=input_data.alert_id,
            tier=tier,
            notes=notes,
        )

    def _classify(self, event: ModelAlertEvent) -> tuple[AlertTier, str]:
        if event.severity in _ESCALATE_SEVERITIES:
            return (
                "ESCALATE",
                f"Severity {event.severity} requires immediate escalation.",
            )

        pattern_lower = event.pattern_matched.lower()
        for keyword in _RECOVERABLE_PATTERNS:
            if keyword in pattern_lower:
                return (
                    "RECOVERABLE",
                    f"Pattern '{event.pattern_matched}' matched recoverable keyword '{keyword}'.",
                )

        if (
            event.restart_count is not None
            and event.restart_count >= _ESCALATE_RESTART_THRESHOLD
        ):
            return (
                "ESCALATE",
                f"Restart storm: restart_count={event.restart_count} >= threshold {_ESCALATE_RESTART_THRESHOLD}.",
            )

        return (
            "UNKNOWN",
            f"Pattern '{event.pattern_matched}' has no known recovery playbook.",
        )

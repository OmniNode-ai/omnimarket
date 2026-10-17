# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local alert compute: every service's latest report and a time in, verdicts and alerts out.

The judgement itself is the shared, pure ``judge_local_services``; this node's
contract holds the rules and the freshness deadline it is given.
"""

from __future__ import annotations

from omnimarket.handlers.handler_local_environment_judgement import (
    judge_local_services,
    load_local_alert_rules,
)
from omnimarket.nodes.node_local_alert_compute.models import (
    ModelLocalAlertRequest,
    ModelLocalAlertResult,
)


class HandlerLocalAlert:
    def handle(self, request: ModelLocalAlertRequest) -> ModelLocalAlertResult:
        verdicts, alerts = judge_local_services(
            request.services, request.observed_at, request.rules
        )
        return ModelLocalAlertResult(verdicts=verdicts, alerts=alerts)


__all__ = ["HandlerLocalAlert", "load_local_alert_rules"]

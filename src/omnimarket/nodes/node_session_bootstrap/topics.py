# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_session_bootstrap.

Declared in contract.yaml publish_topics / subscribe_topics. Import these
constants in handler code — never inline topic strings directly.
"""

from __future__ import annotations

TOPIC_SESSION_BOOTSTRAP_START = "onex.cmd.omnimarket.session-bootstrap-start.v2"
TOPIC_SESSION_BOOTSTRAP_COMPLETED = "onex.evt.omnimarket.session-bootstrap-completed.v2"
TOPIC_SESSION_CRON_HEALTH_VIOLATION = (
    "onex.evt.omnimarket.session-cron-health-violation.v1"
)

__all__: list[str] = [
    "TOPIC_SESSION_BOOTSTRAP_COMPLETED",
    "TOPIC_SESSION_BOOTSTRAP_START",
    "TOPIC_SESSION_CRON_HEALTH_VIOLATION",
]

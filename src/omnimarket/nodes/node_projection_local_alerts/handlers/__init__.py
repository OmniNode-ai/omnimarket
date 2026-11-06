# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local alerts projection: its pure fold plan and its bus writer.

The fold itself lives in ``omnimarket.handlers`` so the transition compute, the
environment writer that publishes transitions and this projection share one
decision without importing each other.
"""

from omnimarket.handlers.handler_local_alert_fold import (
    LOCAL_ALERTS_KEY,
    LOCAL_ALERTS_TABLE,
    plan_local_alert_fold,
)

from .handler_local_alerts_writer import (
    HandlerLocalAlertsProjectionRunner,
    LocalAlertsProjectionWriter,
)
from .handler_projection_local_alerts import HandlerProjectionLocalAlerts

__all__ = [
    "LOCAL_ALERTS_KEY",
    "LOCAL_ALERTS_TABLE",
    "HandlerLocalAlertsProjectionRunner",
    "HandlerProjectionLocalAlerts",
    "LocalAlertsProjectionWriter",
    "plan_local_alert_fold",
]

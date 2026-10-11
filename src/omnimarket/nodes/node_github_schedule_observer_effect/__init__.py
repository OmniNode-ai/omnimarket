# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_github_schedule_observer_effect: reports GitHub scheduled workflows' runs.

The GitHub half of the automation-liveness observers (OMN-20803). Hourly, it
derives each scheduled workflow and its cron from the clones, reads the
scheduled runs, the daily workflow states and the closed pull requests through
the shared GitHub landing transport, and publishes run, verdict and heartbeat
events of the automation-liveness seam. An unreadable source is reported
UNOBSERVABLE, never as zero runs.
"""

from omnimarket.nodes.node_github_schedule_observer_effect.handlers.handler_github_schedule_observer import (
    HandlerGithubScheduleObserver,
)
from omnimarket.nodes.node_github_schedule_observer_effect.models.model_github_schedule_observer_request import (
    ModelGithubScheduleObserverRequest,
)

__all__: list[str] = [
    "HandlerGithubScheduleObserver",
    "ModelGithubScheduleObserverRequest",
]

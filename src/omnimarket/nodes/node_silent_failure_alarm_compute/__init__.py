# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The silent-failure alarms' pure decisions as a compute node (OMN-17427)."""

from .handlers.handler_drainer_alarm import HandlerDrainerAlarm as HandlerDrainerAlarm
from .handlers.handler_job_exit_alarm import HandlerJobExitAlarm as HandlerJobExitAlarm
from .handlers.handler_placement_share_alarm import LANE as LANE
from .handlers.handler_placement_share_alarm import (
    HandlerPlacementShareAlarm as HandlerPlacementShareAlarm,
)
from .handlers.handler_quiet_host_alarm import (
    HandlerQuietHostAlarm as HandlerQuietHostAlarm,
)
from .handlers.handler_quiet_host_alarm import host_id as host_id
from .handlers.handler_throughput_results_alarm import (
    HandlerThroughputResultsAlarm as HandlerThroughputResultsAlarm,
)
from .handlers.handler_tick_liveness_alarm import (
    HandlerTickLivenessAlarm as HandlerTickLivenessAlarm,
)
from .node import NodeSilentFailureAlarmCompute as NodeSilentFailureAlarmCompute

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared wire models for the silent-failure compute and effect nodes."""

from .model_drainer_alarm import ModelDrainerObservation as ModelDrainerObservation
from .model_drainer_alarm import ModelDrainerRepair as ModelDrainerRepair
from .model_drainer_alarm import ModelDrainerRequest as ModelDrainerRequest
from .model_drainer_alarm import ModelDrainerResult as ModelDrainerResult
from .model_job_exit_alarm import JobExitCause as JobExitCause
from .model_job_exit_alarm import ModelJobExitAlarmRequest as ModelJobExitAlarmRequest
from .model_job_exit_alarm import ModelJobExitAlarmResult as ModelJobExitAlarmResult
from .model_job_exit_alarm import ModelJobExitEpisode as ModelJobExitEpisode
from .model_job_exit_alarm import ModelJobExitReading as ModelJobExitReading
from .model_job_exit_alarm import ModelJobExitStreak as ModelJobExitStreak
from .model_placement_share_alarm import ModelLaneRefusal as ModelLaneRefusal
from .model_placement_share_alarm import ModelPlacementRun as ModelPlacementRun
from .model_placement_share_alarm import (
    ModelPlacementShareRequest as ModelPlacementShareRequest,
)
from .model_placement_share_alarm import (
    ModelPlacementShareResult as ModelPlacementShareResult,
)
from .model_quiet_host_alarm import ModelHostActivity as ModelHostActivity
from .model_quiet_host_alarm import ModelQuietHostRequest as ModelQuietHostRequest
from .model_quiet_host_alarm import ModelQuietHostResult as ModelQuietHostResult
from .model_throughput_results_alarm import (
    ModelThroughputResultsRequest as ModelThroughputResultsRequest,
)
from .model_throughput_results_alarm import (
    ModelThroughputResultsResult as ModelThroughputResultsResult,
)
from .model_tick_liveness_alarm import (
    ModelTickLivenessRequest as ModelTickLivenessRequest,
)
from .model_tick_liveness_alarm import (
    ModelTickLivenessResult as ModelTickLivenessResult,
)

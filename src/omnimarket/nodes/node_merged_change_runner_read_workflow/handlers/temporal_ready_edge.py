# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Temporal schedule entrypoint for the dev readiness probe and idle check."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

READY_WORKFLOW_NAME = "MergedChangeRunnerReadReadyEdge"
READY_ACTIVITY_NAME = "runner_read_ready_edge_tick"


@workflow.defn(name=READY_WORKFLOW_NAME)
class MergedChangeRunnerReadReadyEdge:
    """The schedule carries the previous tick's result across runs and worker restarts."""

    @workflow.run
    async def run(self, tick_timeout_seconds: int) -> str:
        # During migration the schedule's last result can be a full-read receipt. The
        # activity recognizes its own versioned state and starts a new baseline otherwise.
        result: str = await workflow.execute_activity(
            READY_ACTIVITY_NAME,
            workflow.get_last_completion_result(str),
            result_type=str,
            start_to_close_timeout=timedelta(seconds=tick_timeout_seconds),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return result

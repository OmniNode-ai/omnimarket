# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sprint closer as a Temporal workflow: one activity lists the open tickets, then one activity per ticket.

The workflow body orders the activities and holds no other logic: each ticket activity reads the ticket, runs
dod_verify, reads the integration ledger, calls the one eligibility rule and writes the decision. A ticket activity
that fails is retried by Temporal on its own; the tickets already finished are in the run's history and never run
again. A run is started by the 2-hour schedule or by a SEEN RUNNING mark (signal-with-start, one workflow per change).
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from omnimarket.nodes.node_sprint_closer_effect.handlers.sprint_closer_sources import (
        ProtocolSprintCloserSources,
    )
    from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
        ModelSprintCloserPlan,
        ModelSprintCloserRunResult,
        ModelTicketOutcome,
    )

WORKFLOW_NAME = "SprintCloserWorkflow"
LIST_ACTIVITY = "sprint_closer_list_tickets"
TICKET_ACTIVITY = "sprint_closer_ticket"
SEEN_RUNNING_SIGNAL = "seen_running"
TASK_QUEUE_DEFAULT = "onex-sprint-closer"


class SprintCloserActivities:
    """The two activities, bound to the sources they read and write through."""

    def __init__(self, sources: ProtocolSprintCloserSources) -> None:
        self._sources = sources

    @activity.defn(name=LIST_ACTIVITY)
    def list_tickets(self, plan_json: str) -> list[str]:
        return self._sources.list_ticket_ids(
            ModelSprintCloserPlan.model_validate_json(plan_json)
        )

    @activity.defn(name=TICKET_ACTIVITY)
    def run_ticket(self, plan_json: str, ticket_id: str) -> str:
        plan = ModelSprintCloserPlan.model_validate_json(plan_json)
        return self._sources.run_ticket(plan, ticket_id).model_dump_json()


@workflow.defn(name=WORKFLOW_NAME)
class SprintCloserWorkflow:
    def __init__(self) -> None:
        self._marks: list[str] = []

    @workflow.signal(name=SEEN_RUNNING_SIGNAL)
    def seen_running(self, change: str) -> None:
        self._marks.append(change)

    @workflow.run
    async def run(self, plan_json: str) -> str:
        plan = ModelSprintCloserPlan.model_validate_json(plan_json)
        retry = RetryPolicy(
            maximum_attempts=plan.activity_max_attempts,
            initial_interval=timedelta(seconds=plan.retry_initial_seconds),
            backoff_coefficient=2.0,
        )
        timeout = timedelta(seconds=plan.activity_timeout_seconds)
        ids: list[str] = await workflow.execute_activity(
            LIST_ACTIVITY,
            args=[plan_json],
            start_to_close_timeout=timeout,
            retry_policy=retry,
            result_type=list[str],
        )
        outcomes: list[ModelTicketOutcome] = []
        for ticket_id in ids:
            raw: str = await workflow.execute_activity(
                TICKET_ACTIVITY,
                args=[plan_json, ticket_id],
                start_to_close_timeout=timeout,
                retry_policy=retry,
                result_type=str,
            )
            outcomes.append(ModelTicketOutcome.model_validate_json(raw))
        return ModelSprintCloserRunResult(
            workflow_id=workflow.info().workflow_id,
            trigger=plan.trigger,
            seen_running_marks=tuple(self._marks),
            outcomes=tuple(outcomes),
        ).model_dump_json()

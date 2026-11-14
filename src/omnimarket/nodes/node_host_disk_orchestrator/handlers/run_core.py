# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One host disk run: read, decide, send the commands, alarm, and later close with the collected results (OMN-23866).

A run starts when the schedule is due or a host reports it is low. It reads each host's newest reading, asks the
decision compute (in process) what to do, and returns the typed command envelopes, one slack-publish envelope per
alarm, and, when no command went out, the run record at once. Otherwise the run stays open in the run book until each
command has its typed result or failure, or its deadline passes: an unanswered command is recorded as
``no_result_by_deadline``, never dropped.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import ValidationError

from omnimarket.models.model_host_disk_decision import (
    ModelHostDiskAlarm,
    ModelHostDiskDecisionRequest,
    ModelHostDiskPolicy,
)
from omnimarket.models.model_host_disk_orchestrator import (
    EnumHostDiskFailureReason,
    EnumHostDiskRunOutcome,
    ModelHostDiskCommandOutcome,
    ModelHostDiskEvictRequested,
    ModelHostDiskOrchestratorOverlay,
    ModelHostDiskRunCompleted,
    run_outcome,
)
from omnimarket.nodes.node_host_disk_decision_compute.handlers.handler_host_disk_decision import (
    decide_host_disk,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.contract_config import (
    evict_requested_topic,
    run_completed_topic,
    slack_publish_topic,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.model_slack_command import (
    ModelHostDiskSlackCommand,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.run_state import (
    HostDiskOrchestratorState,
    OpenRun,
    summarise,
)

Trigger = Literal["scheduled", "low_disk"]
Envelope = ModelEventEnvelope[object]


def _envelope(payload: object, correlation_id: uuid.UUID, topic: str) -> Envelope:
    return ModelEventEnvelope(
        payload=payload, correlation_id=correlation_id, event_type=topic
    )


def _alarm_text(alarm: ModelHostDiskAlarm) -> str:
    return f"[host-disk] {alarm.kind.value}: {alarm.detail}"


def start_run(
    state: HostDiskOrchestratorState,
    overlay: ModelHostDiskOrchestratorOverlay,
    policy: ModelHostDiskPolicy,
    *,
    now: datetime,
    trigger: Trigger,
    run_key: str,
) -> tuple[Envelope, ...]:
    """Start one run; return the envelopes to publish."""
    now = now.astimezone(UTC)
    run_id = f"{overlay.job_id}-{run_key}"
    correlation_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"omnimarket:host-disk-run:{run_id}"
    )
    readings = state.rows.snapshot()
    try:
        decision = decide_host_disk(
            ModelHostDiskDecisionRequest(now=now, policy=policy, readings=readings)
        )
    except ValidationError as exc:
        refused = ModelHostDiskRunCompleted(
            run_id=run_id,
            trigger=trigger,
            started_at=now,
            finished_at=now,
            practice_mode=overlay.practice_mode,
            outcome=EnumHostDiskRunOutcome.POLICY_REFUSED,
            detail=f"policy refused: {len(exc.errors())} error(s): "
            + "; ".join(str(e.get("msg", "")) for e in exc.errors())[:1500],
        )
        return (_envelope(refused, correlation_id, run_completed_topic()),)
    deadline = now + timedelta(seconds=overlay.result_deadline_seconds)
    requests = tuple(
        ModelHostDiskEvictRequested(
            run_id=run_id,
            idempotency_key=c.idempotency_key,
            practice_mode=overlay.practice_mode,
            deadline_at=deadline,
            command=c,
        )
        for c in decision.commands
    )
    hour = now.strftime("%Y-%m-%dT%H")
    alarm_envelopes = tuple(
        _envelope(
            ModelHostDiskSlackCommand(
                channel=overlay.alarm_channel,
                text=_alarm_text(a),
                idempotency_key=f"{a.idempotency_key}|{hour}",
                correlation_id=correlation_id,
            ),
            correlation_id,
            slack_publish_topic(),
        )
        for a in decision.alarms
    )
    run = ModelHostDiskRunCompleted(
        run_id=run_id,
        trigger=trigger,
        started_at=now,
        finished_at=now,
        practice_mode=overlay.practice_mode,
        outcome=EnumHostDiskRunOutcome.NOTHING_DUE,
        readings=summarise(
            readings, {r.host_name: policy.volume_for(r.host_name) for r in readings}
        ),
        decision=decision,
        commands=requests,
        alarms_sent=decision.alarms,
    )
    command_envelopes = tuple(
        _envelope(r, correlation_id, evict_requested_topic()) for r in requests
    )
    if not requests:
        return (*alarm_envelopes, _envelope(run, correlation_id, run_completed_topic()))
    state.runs.open(OpenRun(run=run, deadline_at=deadline))
    return (*command_envelopes, *alarm_envelopes)


def close_run(open_run: OpenRun, *, finished_at: datetime) -> Envelope:
    """Close an open run: each command without an outcome fails as ``no_result_by_deadline``."""
    outcomes = dict(open_run.outcomes)
    for waiting in open_run.waiting_for():
        outcomes[waiting.idempotency_key] = ModelHostDiskCommandOutcome(
            idempotency_key=waiting.idempotency_key,
            host_name=waiting.command.host_name,
            class_name=waiting.command.class_name,
            status="failed",
            failure_reason=EnumHostDiskFailureReason.NO_RESULT_BY_DEADLINE,
            detail=f"no result by {open_run.deadline_at.isoformat()}",
        )
    ordered = tuple(outcomes[c.idempotency_key] for c in open_run.run.commands)
    closed = open_run.run.model_copy(
        update={
            "finished_at": finished_at.astimezone(UTC),
            "results": ordered,
            "outcome": run_outcome(ordered),
        }
    )
    correlation_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"omnimarket:host-disk-run:{closed.run_id}"
    )
    return _envelope(closed, correlation_id, run_completed_topic())

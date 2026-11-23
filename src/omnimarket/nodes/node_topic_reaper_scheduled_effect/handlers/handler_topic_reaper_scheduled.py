# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topic reaper's caller: one dry-run reading of the broker on the runtime's tick (OMN-26438).

``node_topic_reaper_compute`` is a pure compute that declares it is called in process. This handler is that
caller. A tick inside the contract's interval returns nothing. The tick that is due loads the lane's reaper
policy; with none, the reaper is still called, with no observations, and answers ``REAPER_POLICY_MISSING``
without the broker being read. With a policy, the broker is read once, the reaper is called with the tick's
time as its clock and the records it returned last run, and ONE line names the counts.

Dry run only: the policy handed to the reaper always has ``enforce`` false, so the reaper hands out no delete
target, and nothing here deletes a topic or calls a delete effect. Deletion stays a separate decision. The
reaper's records are held in memory; a restart starts them again, which only delays a flag.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_topic_reaper_compute.handlers.handler_topic_reaper import (
    HandlerTopicReaper,
)
from omnimarket.nodes.node_topic_reaper_compute.models.model_topic_reaper import (
    ModelReaperTopicState,
    ModelTopicReaperRequest,
    ModelTopicReaperResult,
)
from omnimarket.nodes.node_topic_reaper_compute.overlay import load_reaper_policy
from omnimarket.nodes.node_topic_reaper_scheduled_effect.adapters.broker_evidence import (
    build_broker_evidence,
)
from omnimarket.nodes.node_topic_reaper_scheduled_effect.protocols import (
    ProtocolReaperEvidence,
    ReaperEvidenceError,
)

NODE_NAME = "node_topic_reaper_scheduled_effect"
CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
#: The lane this caller runs for; the reaper policy of the lane overlay is keyed by it.
LANE = "dev"
_LOG = logging.getLogger(__name__)


class ProtocolReaper(Protocol):
    def handle(self, request: ModelTopicReaperRequest) -> ModelTopicReaperResult: ...


def load_schedule(path: Path = CONTRACT_PATH) -> ModelScheduleDeclaration:
    """The cadence the contract declares; it is a cadence, not a reaper policy value."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelScheduleDeclaration.model_validate(contract["schedule"])


class HandlerTopicReaperScheduled:
    """Gate on the schedule, read the broker once, call the reaper, log one line, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        reaper: ProtocolReaper | None = None,
        evidence: Callable[[], ProtocolReaperEvidence] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lane: str = LANE,
    ) -> None:
        self._gate = gate or ScheduleGate(NODE_NAME, declaration=load_schedule())
        self._reaper: ProtocolReaper = reaper or HandlerTopicReaper()
        self._environ = environ
        self._evidence = evidence or self._live_evidence
        self._clock = clock
        self._lane = lane
        self._state: tuple[ModelReaperTopicState, ...] = ()

    def _live_evidence(self) -> ProtocolReaperEvidence:
        import os

        return build_broker_evidence(
            os.environ if self._environ is None else self._environ
        )

    def _event(
        self,
        request: ModelRuntimeTick,
        outcome: Literal["completed", "failed"],
        detail: str,
    ) -> ModelScheduleRunEvent:
        return self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=self._clock(),
            outcome=outcome,
            detail=detail,
        )

    def _summary(self, status: str, **cells: object) -> str:
        parts = [f"lane={self._lane}", f"status={status}"]
        parts += [f"{name}={value}" for name, value in cells.items()]
        parts.append("dry_run=true")
        return "topic reaper run: " + " ".join(parts)

    def handle(self, request: ModelRuntimeTick) -> ModelScheduleRunEvent | None:
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        policy = load_reaper_policy(self._lane, self._environ)
        if policy is None:
            result = self._reaper.handle(
                ModelTopicReaperRequest(
                    now=now, policy=None, observations=(), boot_set_topics=()
                )
            )
            line = self._summary(
                result.status,
                report_code=result.report_code,
                observed=0,
                flagged=result.flagged_topics,
                deletable=result.deletable_topics,
            )
            _LOG.info(line)
            return self._event(request, "completed", line)
        try:
            evidence = self._evidence().read()
        except (ReaperEvidenceError, ValueError, OSError) as exc:
            line = self._summary(
                "evidence_unreadable", reason=f"{type(exc).__name__}:{exc}"
            )
            _LOG.info(line)
            return self._event(request, "failed", line)
        result = self._reaper.handle(
            ModelTopicReaperRequest(
                now=now,
                policy=policy.model_copy(update={"enforce": False}),
                observations=evidence.observations,
                state=self._state,
                boot_set_topics=evidence.boot_set_topics,
            )
        )
        self._state = result.state
        line = self._summary(
            result.status,
            report_code=result.report_code,
            observed=len(evidence.observations),
            flagged=result.flagged_topics,
            deletable=result.deletable_topics,
            deletable_partitions=result.deletable_partitions,
            held_unread=result.held_unread,
        )
        _LOG.info(line)
        return self._event(request, "completed", line)


__all__: list[str] = ["HandlerTopicReaperScheduled", "load_schedule"]

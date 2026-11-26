# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLaneWorkIntake: effect completions, PR observations and ticks become typed inputs (OMN-26721).

Each subscription other than the ingress topic lands here. The handler names the result
by its model, finds the workflow it answers (by action id, correlation id or change) and
returns one ingress input for the ingress topic; it never folds. The fold classifies the
result against the workflow's state, so a stale observation is dropped there.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from omnibase_core.enums.enum_lane_work_event import EnumLaneWorkEvent
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.lane_work import ModelLaneWorkEvent
from pydantic import BaseModel

from omnimarket.nodes.node_lane_work_orchestrator.handlers.handler_lane_work_orchestrator import (
    HandlerLaneWorkOrchestrator,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_ingress import (
    ModelLaneWorkIngress,
)
from omnimarket.nodes.node_lane_work_orchestrator.process_state import (
    process_orchestrator,
)


def _moment(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class HandlerLaneWorkIntake:
    """``handle(message) -> [ModelLaneWorkIngress]``: zero or more typed inputs for the fold."""

    def __init__(self, orchestrator: HandlerLaneWorkOrchestrator | None = None) -> None:
        self._orchestrator = orchestrator

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["ORCHESTRATOR"]:
        return "ORCHESTRATOR"

    @property
    def orchestrator(self) -> HandlerLaneWorkOrchestrator:
        return self._orchestrator or process_orchestrator()

    def handle(self, request: BaseModel) -> list[ModelLaneWorkIngress]:
        name = type(request).__name__
        data = request.model_dump(mode="json")
        if name == "ModelLabWorkUnitReceipt":
            return self._lab(data)
        if name in ("ModelRepoWriteCompleted", "ModelRepoWriteFailed"):
            return self._repo_write(data, name == "ModelRepoWriteCompleted")
        if name in ("ModelPrHandoffHandedOff", "ModelPrHandoffFailed"):
            return self._handoff(data, name == "ModelPrHandoffHandedOff")
        if name == "ModelWorkLedgerQueryResult":
            return self._ledger_query(data)
        if name in ("ModelLaneWorkObservation", "ModelPrStateObservedEvent"):
            return self._observation(data)
        if name == "ModelRuntimeTick":
            return self._tick(_moment(data["now"]))
        return []  # a ledger append receipt is an acknowledgement, never an event

    async def handle_async(self, request: BaseModel) -> ModelHandlerOutput[None]:
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=getattr(request, "correlation_id", None) or uuid4(),
            handler_id="node_lane_work_orchestrator.intake",
            events=tuple(self.handle(request)),
        )

    # ---------------------------------------------------------------- results

    def _ingress(
        self,
        workflow_id: str,
        result_kind: str,
        payload: dict[str, object],
        input_id: str,
    ) -> ModelLaneWorkIngress:
        return ModelLaneWorkIngress(
            correlation_id=uuid4(),
            workflow_id=workflow_id,
            result_kind=result_kind,
            payload=payload,
            input_id=input_id,
        )

    def _lab(self, data: dict[str, object]) -> list[ModelLaneWorkIngress]:
        action_id = str(data["work_unit_id"])
        workflow_id = self.orchestrator.workflow_for_action(action_id)
        if workflow_id is None:
            return []
        payload = {
            "action_id": action_id,
            "status": data["status"],
            "exit_code": data.get("exit_code"),
            "head_sha": data.get("commit_sha"),
        }
        return [
            self._ingress(
                workflow_id,
                "lab_work_unit_result",
                payload,
                f"lab-{action_id}-{data['status']}",
            )
        ]

    def _repo_write(
        self, data: dict[str, object], completed: bool
    ) -> list[ModelLaneWorkIngress]:
        action_id = str(data["idempotency_key"])
        workflow_id = self.orchestrator.workflow_for_action(action_id)
        if workflow_id is None:
            return []
        status = "completed" if completed else "failed"
        return [
            self._ingress(
                workflow_id,
                "github_write_result",
                {"action_id": action_id, "status": status},
                f"gh-{action_id}-{status}",
            )
        ]

    def _handoff(
        self, data: dict[str, object], handed_off: bool
    ) -> list[ModelLaneWorkIngress]:
        found = self.orchestrator.workflow_for_correlation(
            UUID(str(data["correlation_id"]))
        )
        if found is None:
            return []
        workflow_id, action_id = found
        outcome = "handed_off" if handed_off else "failed"
        payload = {
            "outcome": outcome,
            "action_id": action_id,
            "request_id": action_id,
            "head_sha": data.get("head_sha") or data.get("live_head_sha"),
        }
        return [
            self._ingress(
                workflow_id,
                "handoff_answer",
                payload,
                f"handoff-{action_id}-{outcome}",
            )
        ]

    def _ledger_query(self, data: dict[str, object]) -> list[ModelLaneWorkIngress]:
        found = self.orchestrator.workflow_for_correlation(
            UUID(str(data["correlation_id"]))
        )
        if found is None:
            return []
        workflow_id, action_id = found
        claims = [
            {**claim, "open": True}
            for claim in data.get("open_claims") or []  # type: ignore[attr-defined]
        ]
        return [
            self._ingress(
                workflow_id,
                "ledger_query_result",
                {"action_id": action_id, "claims": claims},
                f"query-{action_id}",
            )
        ]

    def _observation(self, data: dict[str, object]) -> list[ModelLaneWorkIngress]:
        repo = str(data["repo"])
        owner, _, short = repo.rpartition("/")
        number = int(str(data["pr_number"]))
        orchestrator = self.orchestrator
        for record in orchestrator.live_workflows():
            change = record.state.change
            if change.pr_number != number or change.repo != short:
                continue
            if owner and owner != change.owner:
                continue
            payload = {
                **data,
                "pr": f"{short}#{number}",
                "ci_verdict": str(data["ci_verdict"]).lower(),
            }
            return [
                self._ingress(
                    record.state.workflow_id,
                    "pr_observation",
                    payload,
                    f"pr-{short}-{number}-{data.get('digest') or data['observed_at']}",
                )
            ]
        return []

    def _tick(self, now: datetime) -> list[ModelLaneWorkIngress]:
        """A ``deadline_passed`` for each armed deadline that is past, keyed by its arming."""
        inputs: list[ModelLaneWorkIngress] = []
        for record in self.orchestrator.live_workflows():
            state = record.state
            for deadline, due in state.deadline_times.items():
                attempt = state.armed_deadlines.get(deadline)
                if attempt is None or _moment(due) > now:
                    continue
                inputs.append(
                    ModelLaneWorkIngress(
                        correlation_id=uuid4(),
                        event=ModelLaneWorkEvent(
                            kind=EnumLaneWorkEvent.DEADLINE_PASSED,
                            workflow_id=state.workflow_id,
                            input_id=f"{state.workflow_id}-deadline-{deadline}-{attempt}",
                            change=state.change,
                            deadline_id=deadline,
                            deadline_attempt=max(attempt, 1),
                            observed_at=now,
                        ),
                    )
                )
        return inputs


__all__: list[str] = ["HandlerLaneWorkIntake"]

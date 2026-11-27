# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The help step: one pinned request per tier, and the answer validated at the lane boundary (OMN-26722).

Pure: no clock, no I/O, no model call. The chain is the one copied into the workflow's state when its start folded,
never re-read, so a store change reaches the next workflow only. The climb is the reducer's; this handler builds the
request for the tier the reducer chose, refuses a chain position that is not above the worker (a Claude large worker
goes straight to blocked), and turns a delegation's result into the typed event the reducer folds plus a receipt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from omnibase_core.enums.enum_lane_work_state import EnumLaneWorkState
from omnibase_core.models.lane_work import ModelLaneHelpAnswer, ModelLaneHelpRequest
from omnibase_core.models.lane_work.model_lane_help_tier import ModelLaneHelpTier
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_lane_work_orchestrator.handlers.handler_lane_work_orchestrator import (
    correlation_of_action,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_help_receipt import (
    ModelLaneHelpReceipt,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_help_requested import (
    ModelLaneHelpDispatched,
    ModelLaneHelpRequested,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_help_io import (
    ModelLaneWorkHelpAnswerInput,
    ModelLaneWorkHelpAnswerResult,
    ModelLaneWorkHelpBuildRequest,
    ModelLaneWorkHelpDispatch,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_ingress import (
    ModelLaneWorkIngress,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_fold_input import (
    ModelLaneWorkFoldInput,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_state import (
    ModelLaneWorkReducerState,
)

# The key the correlation id is derived under: the same help action always carries the same id, so a replay re-sends
# the same request and only the run this request started can answer it.
_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def correlation_for(action_id: str) -> UUID:
    """The correlation id of the delegation run a help action starts: the orchestrator's own
    derivation, so the intake handler finds the action a harness result answers."""
    return correlation_of_action(action_id)


def first_responder_index(
    tiers: tuple[ModelLaneHelpTier, ...], worker_tier: str | None
) -> int:
    """The tier above the worker's own; a worker the chain does not name is answered from its first tier."""
    names = [tier.name for tier in tiers]
    return names.index(worker_tier) + 1 if worker_tier in names else 0


def _current_tier(state: ModelLaneWorkReducerState) -> ModelLaneHelpTier | None:
    """The tier this help action is pinned to, or None when the chain has no tier above the worker."""
    tiers = state.help_tiers
    if not tiers or state.help_tier_index >= len(tiers):
        return None
    if state.help_tier_index < first_responder_index(tiers, state.worker_tier):
        # The reducer clamps at the top of the chain: a top-tier worker would ask itself.
        return None
    return tiers[state.help_tier_index]


def _asking_tier(state: ModelLaneWorkReducerState) -> str:
    """The worker's tier for the first request of an episode, the tier that just failed for a climb."""
    first = first_responder_index(state.help_tiers, state.worker_tier)
    if state.help_tier_index > first:
        return state.help_tiers[state.help_tier_index - 1].name
    return state.worker_tier or "worker"


def _prompt(request: ModelLaneHelpRequest) -> str:
    schema = json.dumps(ModelLaneHelpAnswer.model_json_schema(), sort_keys=True)
    body = request.model_dump_json(exclude={"tier"})
    return (
        f"A lane work workflow is stuck. Answer the one question below.\n\n{body}\n\n"
        f"Reply with one JSON object only, valid against this JSON schema:\n{schema}"
    )


class ProtocolLaneHelpPorts(Protocol):
    """The handler's two writes and the readback that makes a redelivery safe (plan 3.6)."""

    def append_ledger(self, request: ModelWorkLedgerAppendRequest) -> None: ...

    def send_delegation(self, request: ModelDelegateSkillRequest) -> None: ...

    def delegations_sent(self) -> frozenset[str]:
        """Action ids already on the delegate-skill topic, read back to its high-water mark."""
        ...


class HandlerLaneWorkHelp:
    """Handles the help topic's message, builds the pinned help request and validates its answer.

    ``handle(request: ModelLaneHelpRequested) -> ModelLaneHelpDispatched`` makes two writes for one
    ``ask_help`` message, in a fixed order: the ledger MSG row, then the pinned delegation request,
    then it answers. Both are keyed by the action id (the ledger append deduplicates by request id,
    the delegation carries the action id as its correlation id), and before the second write it reads
    the delegate-skill topic back, so a redelivery after a crash between the writes leaves one row and
    one delegation, and one after both leaves one of each.
    """

    def __init__(
        self,
        ports: ProtocolLaneHelpPorts | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ports = ports
        self._clock = clock

    def handle(self, request: ModelLaneHelpRequested) -> ModelLaneHelpDispatched:
        ports = self._ports
        if ports is None:
            raise RuntimeError("the help handler has no ports wired")
        tier = request.tier
        origin = EnumLaneWorkState(request.help_origin)
        correlation = correlation_for(request.action_id)
        row = (
            f"{request.requested_at.strftime('%Y-%m-%dT%H:%M:%SZ')} | MSG | lane={request.requesting_lane}"
            f" | to={tier.name} | workflow={request.workflow_id} | action={request.action_id}"
            f" | origin={origin.value} | {request.question}"
        )
        # Write 1: the ledger row, keyed by the action id; a redelivery's second append is a duplicate.
        ports.append_ledger(
            ModelWorkLedgerAppendRequest(
                request_id=correlation,
                rows=row,
                requested_by_lane=request.requesting_lane,
                requesting_host=request.requesting_host,
                requested_at=request.requested_at,
            )
        )
        if request.no_responder:
            return ModelLaneHelpDispatched(
                workflow_id=request.workflow_id,
                action_id=request.action_id,
                tier_name=tier.name,
                ledger_written=True,
                delegation_sent=False,
                ingress=ModelLaneWorkIngress(
                    correlation_id=correlation,
                    workflow_id=request.workflow_id,
                    result_kind="delegation_harness_result",
                    payload={
                        "action_id": request.action_id,
                        "request_id": str(correlation),
                        "status": "refused",
                        "ok": False,
                        "detail": "no tier above the worker",
                    },
                    input_id=f"{request.action_id}-no-responder",
                ),
            )
        help_request = ModelLaneHelpRequest(
            workflow_id=request.workflow_id,
            state=EnumLaneWorkState.HELP_REQUESTED,
            help_origin=origin,
            question=request.question,
            tier=tier,
        )
        # Write 2: the pinned delegation, unless its action id is already on the topic.
        already = request.action_id in ports.delegations_sent()
        if not already:
            ports.send_delegation(
                ModelDelegateSkillRequest(
                    prompt=_prompt(help_request),
                    task_type="reasoning",
                    source="claude-code",
                    wait=False,
                    backend_id=tier.backend_id,
                    correlation_id=correlation,
                    metadata={
                        "help_tier": tier.name,
                        "harness": tier.harness,
                        "model": tier.model,
                        "effort": tier.effort,
                        "workflow_id": request.workflow_id,
                        "action_id": request.action_id,
                    },
                )
            )
        return ModelLaneHelpDispatched(
            workflow_id=request.workflow_id,
            action_id=request.action_id,
            tier_name=tier.name,
            backend_id=tier.backend_id,
            ledger_written=True,
            delegation_sent=not already,
        )

    def build(
        self, request: ModelLaneWorkHelpBuildRequest
    ) -> ModelLaneWorkHelpDispatch:
        state, intent = request.state, request.intent
        tier = _current_tier(state)
        origin = EnumLaneWorkState(state.help_origin or intent.inputs["help_origin"])
        asking = _asking_tier(state)
        # A top-tier worker has no tier above it: report it as a help request pinned to its own tier, never sent.
        shown = (
            tier
            or state.help_tiers[min(state.help_tier_index, len(state.help_tiers) - 1)]
        )
        help_request = ModelLaneHelpRequest(
            workflow_id=state.workflow_id,
            state=EnumLaneWorkState.HELP_REQUESTED,
            help_origin=origin,
            last_receipts=request.last_receipts,
            evidence_paths=request.evidence_paths,
            five_whys=request.five_whys,
            question=request.question,
            tier=shown,
        )
        message = (
            f"help request {intent.action_id} for {state.workflow_id}: "
            f"{asking} asks {shown.name} from {origin.value}: {request.question}"
        )
        if tier is None:
            return ModelLaneWorkHelpDispatch(
                help_request=help_request,
                ledger_message=message,
                fold_event=ModelLaneWorkFoldInput(
                    kind="help_failed",
                    input_id=f"{intent.action_id}-no-responder",
                    action_id=intent.action_id,
                    reason="help_exhausted",
                    decision_needed=request.question,
                ),
                receipt=ModelLaneHelpReceipt(
                    workflow_id=state.workflow_id,
                    action_id=intent.action_id,
                    asking_tier=asking,
                    answering_tier=shown.name,
                    configured_effort=shown.effort,
                    outcome="help_failed",
                    detail="no tier above the worker",
                ),
            )
        delegate = ModelDelegateSkillRequest(
            prompt=_prompt(help_request),
            task_type="reasoning",
            source="claude-code",
            wait=False,
            backend_id=tier.backend_id,
            correlation_id=correlation_for(intent.action_id),
            metadata={
                "help_tier": tier.name,
                "harness": tier.harness,
                "model": tier.model,
                "effort": tier.effort,
                "workflow_id": state.workflow_id,
                "action_id": intent.action_id,
            },
        )
        return ModelLaneWorkHelpDispatch(
            help_request=help_request, delegate_request=delegate, ledger_message=message
        )

    def validate_answer(
        self, request: ModelLaneWorkHelpAnswerInput
    ) -> ModelLaneWorkHelpAnswerResult:
        """Parse a delegation result into the typed event; anything else is `help_failed`."""
        state = request.state
        action_id = next(
            (
                a
                for a, name in state.outstanding_actions.items()
                if name == "help_request"
            ),
            None,
        )
        tier = _current_tier(state)
        if action_id is None or tier is None:
            raise ValueError("no help request is outstanding in this state")

        def receipt(outcome: str, detail: str | None = None) -> ModelLaneHelpReceipt:
            return ModelLaneHelpReceipt(
                workflow_id=state.workflow_id,
                action_id=action_id,
                asking_tier=_asking_tier(state),
                answering_tier=tier.name,
                executed_backend_id=request.executed_backend_id,
                executed_model=request.executed_model,
                configured_effort=tier.effort,
                outcome=outcome,
                detail=detail,
            )

        if request.correlation_id != str(correlation_for(action_id)):
            return ModelLaneWorkHelpAnswerResult(
                receipt=receipt(
                    "refused", "answer is from a run this orchestrator did not start"
                )
            )

        def failed(detail: str) -> ModelLaneWorkHelpAnswerResult:
            return ModelLaneWorkHelpAnswerResult(
                fold_event=ModelLaneWorkFoldInput(
                    kind="help_failed",
                    input_id=f"{action_id}-failed",
                    action_id=action_id,
                ),
                receipt=receipt("help_failed", detail),
            )

        if request.failed:
            return failed("the delegation failed")
        if (request.executed_backend_id, request.executed_model) != (
            tier.backend_id,
            tier.model,
        ):
            return failed("the attempt record's backend or model is not the tier's")
        try:
            answer = ModelLaneHelpAnswer.model_validate_json(
                _FENCE.sub("", request.answer_text.strip())
            )
        except ValidationError:
            return failed("the answer does not parse as a help answer")
        event = ModelLaneWorkFoldInput(
            kind=answer.kind,
            input_id=f"{action_id}-answer",
            action_id=action_id,
            evidence=tuple(text for text in (answer.cause, answer.correction) if text),
            help_origin=answer.help_origin.value if answer.help_origin else None,
            reason=answer.reason.value if answer.reason else None,
            decision_needed=answer.decision_needed,
        )
        return ModelLaneWorkHelpAnswerResult(
            fold_event=event, receipt=receipt(answer.kind)
        )


__all__ = [
    "HandlerLaneWorkHelp",
    "correlation_for",
    "first_responder_index",
]

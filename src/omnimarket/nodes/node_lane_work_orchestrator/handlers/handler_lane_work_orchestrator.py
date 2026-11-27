# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLaneWorkOrchestrator: the one fold path of the lane work loop (OMN-26721).

For each ingress input: classify (when it is a raw or typed result), fold through the
lane work reducer, publish the transition receipt (the commit point), publish each
output the receipt lists, and return so the runtime commits the input. A crash before
the commit replays the input; the fold finds its ``input_id`` in the receipts and
publishes only the outputs of that receipt not yet on their topics, matched by output
id. The handler names roles; the contract names topics (``event_topics``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from omnibase_core.enums.enum_lane_work_blocked_reason import EnumLaneWorkBlockedReason
from omnibase_core.enums.enum_lane_work_event import EnumLaneWorkEvent
from omnibase_core.enums.enum_lane_work_state import EnumLaneWorkState
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.lane_work import (
    ModelLaneHelpTier,
    ModelLaneWorkAction,
    ModelLaneWorkEvent,
    ModelLaneWorkReceipt,
    ModelLaneWorkState,
    ModelLaneWorkStepReply,
    ModelLaneWorkTerminal,
    workflow_id_for,
)
from pydantic import BaseModel

from omnimarket.models.model_repo_write_io import (
    EnumRepoWriteOperation,
    ModelRepoWriteRequest,
)
from omnimarket.models.overlay_documents.model_lane_work_orchestrator_overlay import (
    ModelLaneWorkOrchestratorOverlay,
    load_orchestrator_overlay,
)
from omnimarket.models.pr_handoff import (
    EnumPrHandoffLabProofSource,
    ModelPrHandoffLabProof,
    ModelPrHandoffRequested,
)
from omnimarket.models.work_ledger_append.model_work_ledger_append import (
    ModelWorkLedgerAppendRequest,
)
from omnimarket.models.work_ledger_query.model_work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_lab_work_unit_effect.models.model_lab_work_unit import (
    ModelLabWorkUnitRequest,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.handlers.handler_lane_work_event_classify import (
    HandlerLaneWorkEventClassify,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.models.model_lane_work_classify_request import (
    RAW_TEXT,
    ModelLaneWorkClassifyRequest,
)
from omnimarket.nodes.node_lane_work_orchestrator.agent_actions import worker_kind
from omnimarket.nodes.node_lane_work_orchestrator.event_topics import (
    ROLE_HANDOFF,
    ROLE_HELP_REQUESTED,
    ROLE_LAB_PROOF,
    ROLE_LEDGER_APPEND,
    ROLE_LEDGER_QUERY,
    ROLE_REPO_WRITE,
    ROLE_STEP_COMPLETED,
    ROLE_TERMINAL,
    ROLE_TRANSITION,
)
from omnimarket.nodes.node_lane_work_orchestrator.journal import (
    InMemoryLaneWorkJournal,
    ProtocolLaneWorkJournal,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_fold_result import (
    ModelLaneWorkFoldResult,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_help_message import (
    ModelLaneWorkHelpMessage,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_ingress import (
    ModelLaneWorkIngress,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_output import (
    ModelLaneWorkOutput,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_step_completed import (
    ModelLaneWorkStepCompleted,
)
from omnimarket.nodes.node_lane_work_orchestrator.models.model_lane_work_transition_record import (
    ModelLaneWorkTransitionRecord,
)
from omnimarket.nodes.node_lane_work_reducer.handlers.handler_lane_work_reducer import (
    HandlerLaneWorkReducer,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_intent import (
    ModelLaneWorkIntent,
)
from omnimarket.nodes.node_lane_work_reducer.models.model_lane_work_reducer_state import (
    ModelLaneWorkReducerState,
)

_NODE_ACTIONS_WITHOUT_OUTPUT = frozenset(
    {
        "arm_worker_deadline",
        "arm_observation_deadline",
        "arm_poll_deadline",
        "arm_confirmation_deadline",
    }
)
_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    ROLE_TRANSITION: ModelLaneWorkTransitionRecord,
    ROLE_STEP_COMPLETED: ModelLaneWorkStepCompleted,
    ROLE_TERMINAL: ModelLaneWorkTerminal,
    ROLE_LEDGER_APPEND: ModelWorkLedgerAppendRequest,
    ROLE_LEDGER_QUERY: ModelWorkLedgerQueryRequest,
    ROLE_LAB_PROOF: ModelLabWorkUnitRequest,
    ROLE_REPO_WRITE: ModelRepoWriteRequest,
    ROLE_HANDOFF: ModelPrHandoffRequested,
    ROLE_HELP_REQUESTED: ModelLaneWorkHelpMessage,
}
_CHANGE_KEY = tuple[str, str, int]


def correlation_of_action(action_id: str) -> UUID:
    """The correlation id every command of one action carries, so its answer finds the action."""
    return uuid5(NAMESPACE_URL, f"lane-work-action:{action_id}")


def _moment(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _line(text: str, limit: int = 300) -> str:
    return " ".join(text.split())[:limit]


class HandlerLaneWorkOrchestrator:
    """Folds one input at a time; ``handle(ModelLaneWorkIngress) -> ModelLaneWorkFoldResult``."""

    def __init__(
        self,
        *,
        journal: ProtocolLaneWorkJournal | None = None,
        overlay: ModelLaneWorkOrchestratorOverlay | None = None,
        reducer: HandlerLaneWorkReducer | None = None,
        classifier: HandlerLaneWorkEventClassify | None = None,
        clock: Callable[[], datetime] | None = None,
        host_picker: Callable[[str, str], str] | None = None,
    ) -> None:
        self.journal: ProtocolLaneWorkJournal = (
            journal if journal is not None else InMemoryLaneWorkJournal()
        )
        self._overlay = overlay if overlay is not None else load_orchestrator_overlay()
        self._reducer_config = self._overlay.reducer_config()
        self._reducer = reducer if reducer is not None else HandlerLaneWorkReducer()
        self._classifier = (
            classifier if classifier is not None else HandlerLaneWorkEventClassify()
        )
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._host_picker = host_picker
        self._history: dict[str, list[ModelLaneWorkTransitionRecord]] = {}
        self._published: set[tuple[str, str]] = set()
        self._lock: asyncio.Lock | None = None

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["ORCHESTRATOR"]:
        return "ORCHESTRATOR"

    # ------------------------------------------------------------ directory

    def known_workflows(self) -> list[str]:
        return list(self._history)

    def newest(self, workflow_id: str) -> ModelLaneWorkTransitionRecord | None:
        records = self._history.get(workflow_id)
        return records[-1] if records else None

    def state_of(self, workflow_id: str) -> ModelLaneWorkReducerState:
        record = self.newest(workflow_id)
        if record is None:
            msg = f"unknown workflow {workflow_id!r}"
            raise KeyError(msg)
        return record.state

    def live_workflows(self) -> list[ModelLaneWorkTransitionRecord]:
        return [
            record
            for records in self._history.values()
            if (record := records[-1]).state.name not in ("handed_off", "blocked")
        ]

    def workflow_for_change(self, owner: str, repo: str, pr_number: int) -> str | None:
        """The live workflow of a change: one live workflow per change."""
        for record in self.live_workflows():
            change = record.state.change
            if (change.owner, change.repo, change.pr_number) == (
                owner,
                repo,
                pr_number,
            ):
                return record.state.workflow_id
        return None

    def workflow_for_action(self, action_id: str) -> str | None:
        for workflow_id, records in self._history.items():
            if action_id in records[-1].state.outstanding_actions:
                return workflow_id
        return None

    def workflow_for_correlation(self, correlation_id: UUID) -> tuple[str, str] | None:
        """The workflow and action a command's correlation id answers."""
        for workflow_id, records in self._history.items():
            for action_id in records[-1].state.outstanding_actions:
                if correlation_of_action(action_id) == correlation_id:
                    return workflow_id, action_id
        return None

    def core_state(self, workflow_id: str) -> ModelLaneWorkState:
        record = self.newest(workflow_id)
        if record is None:
            msg = f"unknown workflow {workflow_id!r}"
            raise KeyError(msg)
        state = record.state
        return ModelLaneWorkState(
            workflow_id=state.workflow_id,
            change=state.change,
            state=EnumLaneWorkState(state.name),
            seq=state.seq,
            attempt=record.attempt,
            worker_lane=state.worker_lane,
            expected_head=state.expected_head,
            last_head_change_at=record.last_head_change_at,
            help_origin=EnumLaneWorkState(state.help_origin)
            if state.help_origin
            else None,
            help_tiers=state.help_tiers,
            help_tier_index=state.help_tier_index,
            help_episodes=state.help_episodes,
            repeat_counters=dict(state.repeat_counters),
            outstanding_action_ids=tuple(state.outstanding_actions),
            seen_input_ids=state.seen_input_ids,
        )

    # ----------------------------------------------------------------- fold

    def handle(self, request: ModelLaneWorkIngress) -> ModelLaneWorkFoldResult:
        """Fold one input and publish its receipt and outputs, in that order."""
        now = self._clock()
        workflow_id = self._workflow_of(request)
        event = request.event
        if workflow_id is None:
            if event is None or event.kind is not EnumLaneWorkEvent.START:
                return ModelLaneWorkFoldResult(outcome="dropped")
            refusal = self._start_refusal(event)
            if refusal is not None:
                return self._refused_start(event, request.correlation_id, refusal)
            workflow_id = self._create(event, now)
        if event is None:
            event_or_result = self._classify(request, workflow_id)
            if isinstance(event_or_result, ModelLaneWorkFoldResult):
                return event_or_result
            event = event_or_result
        return self._fold(workflow_id, event, request.correlation_id, now)

    async def handle_async(
        self, request: ModelLaneWorkIngress
    ) -> ModelHandlerOutput[None]:
        """Runtime entrypoint: what this call published, as typed events, in order."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            result = self.handle(request)
        events = tuple(
            _PAYLOAD_MODELS[output.role].model_validate(output.payload)
            for output in result.published
        )
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_lane_work_orchestrator.fold",
            events=events,
        )

    def _start_refusal(self, event: ModelLaneWorkEvent) -> str | None:
        """A start names a worker tier the chain holds; there is no default tier, so a missing
        or unknown one refuses the start and no workflow begins (plan 3.5, spike 2 finding 4)."""
        chain = tuple(tier.name for tier in self._overlay.help_tiers)
        if event.worker_tier is not None and event.worker_tier in chain:
            return None
        return (
            f"worker_tier_not_in_chain: worker_tier {event.worker_tier!r} is not one of "
            f"the chain's ids ({', '.join(chain)})"
        )

    def _refused_start(
        self, event: ModelLaneWorkEvent, correlation_id: UUID, refusal: str
    ) -> ModelLaneWorkFoldResult:
        """The answer to a refused start: the caller's correlation id and the reason, no workflow."""
        answer = ModelLaneWorkStepCompleted(
            correlation_id=correlation_id, refusal=refusal
        )
        output = ModelLaneWorkOutput(
            output_id=f"refused-{event.input_id}"[:160],
            role=ROLE_STEP_COMPLETED,
            payload=answer.model_dump(mode="json"),
        )
        return ModelLaneWorkFoldResult(
            outcome="refused",
            refusal=refusal,
            published=self._publish_new((output,)),
        )

    def _workflow_of(self, request: ModelLaneWorkIngress) -> str | None:
        event = request.event
        if event is None:
            return request.workflow_id if request.workflow_id in self._history else None
        if event.kind is EnumLaneWorkEvent.START:
            for workflow_id, records in self._history.items():
                if event.input_id in records[-1].state.seen_input_ids:
                    return workflow_id  # a redelivered start, even after its terminal
            return self.workflow_for_change(
                event.change.owner, event.change.repo, event.change.pr_number
            )
        return event.workflow_id if event.workflow_id in self._history else None

    def _create(self, event: ModelLaneWorkEvent, now: datetime) -> str:
        change = event.change
        prior = sum(
            1
            for records in self._history.values()
            if records[0].state.change.repo == change.repo
            and records[0].state.change.owner == change.owner
            and records[0].state.change.pr_number == change.pr_number
        )
        workflow_id = workflow_id_for(
            change.owner, change.repo, change.pr_number, prior + 1
        )
        initial = self._reducer.initial_state(
            config=self._reducer_config, workflow_id=workflow_id, change=change
        )
        seed = ModelLaneWorkTransitionRecord(
            receipt=ModelLaneWorkReceipt(
                workflow_id=workflow_id,
                seq=1,
                receipt_id=f"{workflow_id}-seed",
                kind="transition",
                input_id=f"{workflow_id}-seed",
                event_kind=EnumLaneWorkEvent.STATUS_REQUESTED,
                from_state=EnumLaneWorkState.QUEUED,
                to_state=EnumLaneWorkState.QUEUED,
                action_ids=(),
                occurred_at=now,
            ),
            correlation_id=uuid4(),
            state=initial,
            attempt=prior + 1,
            started_at=now,
        )
        self._history[workflow_id] = [seed]
        return workflow_id

    def _classify(
        self, request: ModelLaneWorkIngress, workflow_id: str
    ) -> ModelLaneWorkEvent | ModelLaneWorkFoldResult:
        state = self.core_state(workflow_id)
        answer = self._classifier.handle(
            ModelLaneWorkClassifyRequest(
                state=state,
                result_kind=str(request.result_kind),
                raw_result=request.raw_result
                if request.result_kind == RAW_TEXT
                else None,
                payload=request.payload,
                input_id=request.input_id,
            )
        )
        if answer.outcome == "event" and answer.event is not None:
            return answer.event
        reply = self._status_reply(workflow_id, request.correlation_id)
        output = self._reply_output(reply, f"{workflow_id}-{state.seq}")
        return ModelLaneWorkFoldResult(
            outcome=answer.outcome,
            reply=reply,
            published=self._publish_new((output,)),
        )

    def _fold(
        self,
        workflow_id: str,
        event: ModelLaneWorkEvent,
        correlation_id: UUID,
        now: datetime,
    ) -> ModelLaneWorkFoldResult:
        before = self.newest(workflow_id)
        assert before is not None
        state = before.state
        if event.input_id in state.seen_input_ids:
            return self._duplicate(before, event.input_id, correlation_id)
        result = self._reducer.reduce(state, event, now, self._reducer_config)
        if result.outcome == "status":
            reply = self._status_reply(workflow_id, correlation_id)
            output = self._reply_output(reply, f"{workflow_id}-{state.seq}")
            return ModelLaneWorkFoldResult(
                outcome="status", reply=reply, published=self._publish_new((output,))
            )
        record = self._record(
            before, event, result.state, result.intents, result, correlation_id, now
        )
        self._history[workflow_id].append(record)
        published = self._ensure_outputs(record)
        return ModelLaneWorkFoldResult(
            outcome="refused" if result.outcome == "refused" else "transition",
            record=record,
            reply=self._reply_of(record),
            published=published,
        )

    def _duplicate(
        self,
        before: ModelLaneWorkTransitionRecord,
        input_id: str,
        correlation_id: UUID,
    ) -> ModelLaneWorkFoldResult:
        """The input was folded: publish what its receipt lists and is not yet on the bus,
        then the current step reply under the caller's new correlation id."""
        workflow_id = before.state.workflow_id
        published: tuple[ModelLaneWorkOutput, ...] = ()
        same_caller = False
        for record in self._history[workflow_id]:
            if record.receipt.input_id == input_id:
                published = self._ensure_outputs(record)
                same_caller = record.correlation_id == correlation_id
        reply = self._status_reply(workflow_id, correlation_id)
        if same_caller:
            # The first call's reply is one of the receipt's outputs, now on the bus.
            return ModelLaneWorkFoldResult(
                outcome="duplicate", record=None, reply=reply, published=published
            )
        output = self._reply_output(reply, f"{workflow_id}-{before.state.seq}")
        return ModelLaneWorkFoldResult(
            outcome="duplicate",
            record=None,
            reply=reply,
            published=(*published, *self._publish_new((output,))),
        )

    # -------------------------------------------------------------- records

    def _record(
        self,
        before: ModelLaneWorkTransitionRecord,
        event: ModelLaneWorkEvent,
        after: ModelLaneWorkReducerState,
        intents: tuple[ModelLaneWorkIntent, ...],
        result: Any,
        correlation_id: UUID,
        now: datetime,
    ) -> ModelLaneWorkTransitionRecord:
        refused = result.outcome == "refused"
        receipt_id = f"{after.workflow_id}-{after.seq}"
        fields: dict[str, Any] = {
            "last_head_change_at": before.last_head_change_at,
            "pending_action": before.pending_action,
            "proof_receipt_id": before.proof_receipt_id,
            "proof_summary": before.proof_summary,
            "claim_prs": before.claim_prs,
            "claim_tickets": before.claim_tickets,
            "body_ticket_ids": before.body_ticket_ids,
            "transitions_taken": before.transitions_taken + (0 if refused else 1),
            "help_requests_by_tier": dict(before.help_requests_by_tier),
            "help_action_ids": before.help_action_ids,
        }
        if not refused:
            fields.update(self._extras(before, event, after, receipt_id, now))
        agent_actions = [
            self._worker_action(intent) for intent in intents if intent.actor == "agent"
        ]
        if not refused:
            fields["pending_action"] = agent_actions[0] if agent_actions else None
        receipt = ModelLaneWorkReceipt(
            workflow_id=after.workflow_id,
            seq=after.seq,
            receipt_id=receipt_id,
            kind="refused" if refused else "transition",
            input_id=event.input_id,
            event_kind=event.kind,
            from_state=EnumLaneWorkState(before.state.name),
            to_state=EnumLaneWorkState(after.name),
            action_ids=tuple(intent.action_id for intent in intents),
            actions=tuple(
                action
                for intent in intents
                if (action := self._core_action(intent)) is not None
            ),
            refusal=result.refusal if refused else None,
            occurred_at=now,
        )
        draft = ModelLaneWorkTransitionRecord(
            receipt=receipt,
            correlation_id=correlation_id,
            state=after,
            attempt=before.attempt,
            started_at=before.started_at,
            **fields,
        )
        outputs, updates = self._outputs(before, draft, event, intents, now)
        return draft.model_copy(update={"outputs": outputs, **updates})

    def _extras(
        self,
        before: ModelLaneWorkTransitionRecord,
        event: ModelLaneWorkEvent,
        after: ModelLaneWorkReducerState,
        receipt_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        extras: dict[str, Any] = {}
        if after.expected_head != before.state.expected_head or event.kind.value in (
            "pushed",
            "head_moved",
        ):
            extras["last_head_change_at"] = now
        kind = event.kind.value
        if kind == "worker_started":
            extras["body_ticket_ids"] = event.body_ticket_ids
        if kind == "claim_confirmed":
            change = after.change
            extras["claim_prs"] = (f"{change.repo}#{change.pr_number}",)
            extras["claim_tickets"] = (change.ticket,) if change.ticket else ()
        if kind == "proof_passed":
            extras["proof_receipt_id"] = receipt_id
            observed = _line(" ".join(event.evidence) or "exit 0", 200)
            extras["proof_summary"] = (
                f"{before.proof_summary or ''} observed={observed}".strip()
            )
        return extras

    @staticmethod
    def _core_action(intent: ModelLaneWorkIntent) -> ModelLaneWorkAction | None:
        if intent.kind == "terminal":
            return None
        name = worker_kind(intent.name) if intent.actor == "agent" else intent.name
        return ModelLaneWorkAction(
            actor="agent" if intent.actor == "agent" else "node",
            action_id=intent.action_id,
            name=name,
            inputs=dict(intent.inputs),
            deadline_at=intent.deadline_at,
        )

    @staticmethod
    def _worker_action(intent: ModelLaneWorkIntent) -> ModelLaneWorkAction:
        return ModelLaneWorkAction(
            actor="agent",
            action_id=intent.action_id,
            name=worker_kind(intent.name),
            inputs={**intent.inputs, "step": intent.name},
            deadline_at=intent.deadline_at,
        )

    # -------------------------------------------------------------- outputs

    def _outputs(
        self,
        before: ModelLaneWorkTransitionRecord,
        record: ModelLaneWorkTransitionRecord,
        event: ModelLaneWorkEvent,
        intents: tuple[ModelLaneWorkIntent, ...],
        now: datetime,
    ) -> tuple[tuple[ModelLaneWorkOutput, ...], dict[str, Any]]:
        state = record.state
        receipt_id = record.receipt.receipt_id
        outputs: list[ModelLaneWorkOutput] = []
        terminal: ModelLaneWorkTerminal | None = None
        commands: list[ModelLaneWorkOutput] = []
        counts = dict(record.help_requests_by_tier)
        helps = list(record.help_action_ids)
        updates: dict[str, Any] = {}
        for intent in intents:
            if intent.actor == "agent" or intent.name in _NODE_ACTIONS_WITHOUT_OUTPUT:
                continue
            if intent.kind == "terminal":
                terminal = self._terminal(before, record, intent, now)
                continue
            if intent.name == "help_request":
                tier = intent.inputs.get("tier", "unnamed")
                counts[tier] = counts.get(tier, 0) + 1
                helps.append(intent.action_id)
            command, extra = self._command(record, intent, now)
            commands.append(command)
            updates.update(extra)
        updates["help_requests_by_tier"] = counts
        updates["help_action_ids"] = tuple(helps)
        reply = self._reply_of(record.model_copy(update=updates), terminal)
        outputs.append(self._reply_output(reply, receipt_id))
        outputs.append(self._status_output(record, event, now))
        outputs.extend(commands)
        if terminal is not None:
            outputs.append(
                ModelLaneWorkOutput(
                    output_id=f"{state.workflow_id}-terminal",
                    role=ROLE_TERMINAL,
                    payload=terminal.model_dump(mode="json"),
                )
            )
        return tuple(outputs), updates

    def _command(
        self,
        record: ModelLaneWorkTransitionRecord,
        intent: ModelLaneWorkIntent,
        now: datetime,
    ) -> tuple[ModelLaneWorkOutput, dict[str, Any]]:
        state = record.state
        change = state.change
        repo = f"{change.owner}/{change.repo}"
        pr_ref = f"{change.repo}#{change.pr_number}"
        correlation = correlation_of_action(intent.action_id)
        head = state.expected_head or ""
        name = intent.name
        role: str
        model: BaseModel
        extra: dict[str, Any] = {}
        if name == "ledger_query_claims":
            role = ROLE_LEDGER_QUERY
            model = ModelWorkLedgerQueryRequest(
                correlation_id=correlation,
                query=EnumWorkLedgerQueryKind.OPEN_CLAIMS,
                lane=state.worker_lane,
                pr=pr_ref,
            )
        elif name == "lab_proof":
            role = ROLE_LAB_PROOF
            argv = self._overlay.proof_commands.get(
                change.repo, self._overlay.proof_commands["default"]
            )
            host = (
                self._host_picker(repo, intent.action_id)
                if self._host_picker is not None
                else self._overlay.proof_target_host
            )
            model = ModelLabWorkUnitRequest(
                work_unit_id=intent.action_id,
                target_host=host,
                repo=repo,
                commit_sha=head,
                argv=list(argv),
                lane=self._overlay.requesting_lane,
            )
            extra["proof_summary"] = (
                f"host={host} lane={state.worker_lane or self._overlay.requesting_lane}"
                f" command={' '.join(argv)}"
            )
        elif name == "post_lab_comment":
            role = ROLE_REPO_WRITE
            model = ModelRepoWriteRequest(
                correlation_id=correlation,
                operation=EnumRepoWriteOperation.PR_COMMENT,
                repo=repo,
                idempotency_key=intent.action_id,
                requested_by_lane=self._overlay.requesting_lane,
                pr_number=change.pr_number,
                body=self._lab_line(record),
            )
        elif name == "handoff_request":
            role = ROLE_HANDOFF
            model = self._handoff(record, intent, correlation, now)
        elif name == "help_request":
            # The `ask_help` action: one message on the help topic. The orchestrator writes no
            # ledger MSG row of its own for help; HandlerLaneWorkHelp does (plan 3.5, finding 3).
            role = ROLE_HELP_REQUESTED
            model = ModelLaneWorkHelpMessage(
                workflow_id=state.workflow_id,
                action_id=intent.action_id,
                state=state.name,
                help_origin=str(intent.inputs.get("help_origin", "")),
                tier=ModelLaneHelpTier(
                    name=str(intent.inputs["tier"]),
                    backend_id=str(intent.inputs["backend_id"]),
                    harness=str(intent.inputs["harness"]),
                    model=str(intent.inputs["model"]),
                    effort=str(intent.inputs["effort"]),
                ),
                question=_line(
                    f"help request on {pr_ref}: "
                    f"{intent.inputs.get('evidence', 'no evidence')}"
                ),
                evidence=_line(str(intent.inputs.get("evidence", "no evidence"))),
                change_ref=pr_ref,
                requesting_lane=self._overlay.requesting_lane,
                requesting_host=self._overlay.requesting_host,
                requested_at=now,
                worker_tier=state.worker_tier,
                no_responder=str(intent.inputs["tier"]) == state.worker_tier,
            )
        else:
            msg = f"node action {name!r} has no command"
            raise KeyError(msg)
        return (
            ModelLaneWorkOutput(
                output_id=intent.action_id,
                role=role,
                payload=model.model_dump(mode="json"),
            ),
            extra,
        )

    def _lab_line(self, record: ModelLaneWorkTransitionRecord) -> str:
        head = record.state.expected_head or ""
        summary = record.proof_summary or "observed=proof passed"
        return f"Lab: head={head} {summary}"

    def _handoff(
        self,
        record: ModelLaneWorkTransitionRecord,
        intent: ModelLaneWorkIntent,
        correlation: UUID,
        now: datetime,
    ) -> ModelPrHandoffRequested:
        state = record.state
        change = state.change
        helps = record.help_action_ids
        delegation = (
            f"delegated={len(helps)} runs={','.join(helps)}"
            if helps
            else "delegated=0 delegation_reason=no help needed"
        )
        return ModelPrHandoffRequested(
            correlation_id=correlation,
            repo=change.repo,
            pr_number=change.pr_number,
            expected_head_sha=intent.inputs.get("expected_head_sha")
            or state.expected_head,
            lane=state.worker_lane or self._overlay.requesting_lane,
            claim_tickets=record.claim_tickets,
            claim_prs=record.claim_prs,
            body_ticket_ids=record.body_ticket_ids,
            lab_proof=ModelPrHandoffLabProof(
                source=EnumPrHandoffLabProofSource.COMMENT, line=self._lab_line(record)
            ),
            delegation=delegation,
            requesting_host=self._overlay.requesting_host,
            requested_at=now,
        )

    def _status_output(
        self,
        record: ModelLaneWorkTransitionRecord,
        event: ModelLaneWorkEvent,
        now: datetime,
    ) -> ModelLaneWorkOutput:
        receipt = record.receipt
        row = (
            f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')} | STATUS | lane={self._overlay.requesting_lane}"
            f" | workflow={receipt.workflow_id} | seq={receipt.seq} | event={event.kind.value}"
            f" | from={receipt.from_state.value} | to={receipt.to_state.value}"
            f" | kind={receipt.kind} | lane work transition"
        )
        request = ModelWorkLedgerAppendRequest(
            request_id=uuid5(NAMESPACE_URL, f"lane-work-status:{receipt.receipt_id}"),
            rows=row,
            requested_by_lane=self._overlay.requesting_lane,
            requesting_host=self._overlay.requesting_host,
            requested_at=now,
        )
        return ModelLaneWorkOutput(
            output_id=f"{receipt.receipt_id}-status",
            role=ROLE_LEDGER_APPEND,
            payload=request.model_dump(mode="json"),
        )

    def _terminal(
        self,
        before: ModelLaneWorkTransitionRecord,
        record: ModelLaneWorkTransitionRecord,
        intent: ModelLaneWorkIntent,
        now: datetime,
    ) -> ModelLaneWorkTerminal:
        state = record.state
        handed_off = state.name == "handed_off"
        common: dict[str, Any] = {
            "workflow_id": state.workflow_id,
            "seq": state.seq,
            "change": state.change,
            "last_state": EnumLaneWorkState(before.state.name),
            "evidence": tuple(
                part for part in intent.inputs.get("evidence", "").split(" | ") if part
            ),
            "transitions_taken": record.transitions_taken,
            "help_requests_by_tier": dict(record.help_requests_by_tier),
            "started_at": record.started_at,
            "ended_at": max(now, record.started_at),
        }
        if handed_off:
            return ModelLaneWorkTerminal(
                kind="handed_off",
                proven_head=state.proven_head or state.expected_head,
                handoff_request_id=before.state.handoff_request_id,
                proof_receipt_id=record.proof_receipt_id,
                **common,
            )
        decision = intent.inputs.get("decision") or "none"
        return ModelLaneWorkTerminal(
            kind="blocked",
            proven_head=state.proven_head,
            reason=EnumLaneWorkBlockedReason(intent.inputs["reason"]),
            decision_needed=decision,
            decision_owner=self._overlay.decision_owner if decision == "none" else None,
            **common,
        )

    # -------------------------------------------------------------- replies

    def _reply_of(
        self,
        record: ModelLaneWorkTransitionRecord,
        terminal: ModelLaneWorkTerminal | None = None,
    ) -> ModelLaneWorkStepCompleted:
        state = record.state
        base: dict[str, Any] = {
            "workflow_id": state.workflow_id,
            "state": EnumLaneWorkState(state.name),
            "seq": state.seq,
            "receipt_id": record.receipt.receipt_id,
        }
        if terminal is None and state.name in ("handed_off", "blocked"):
            terminal = self._terminal_of(state.workflow_id)
        if terminal is not None:
            reply = ModelLaneWorkStepReply(
                next_action="stop", terminal=terminal, **base
            )
        elif record.pending_action is not None:
            reply = ModelLaneWorkStepReply(
                next_action="agent_action", action=record.pending_action, **base
            )
        else:
            reply = ModelLaneWorkStepReply(
                next_action="wait", wait_deadline=self._wait_until(record), **base
            )
        return ModelLaneWorkStepCompleted(
            correlation_id=record.correlation_id, reply=reply
        )

    def _terminal_of(self, workflow_id: str) -> ModelLaneWorkTerminal | None:
        for output_id, payload in self.journal.read(ROLE_TERMINAL):
            if output_id == f"{workflow_id}-terminal":
                return ModelLaneWorkTerminal.model_validate(payload)
        for record in reversed(self._history.get(workflow_id, [])):
            for output in record.outputs:
                if output.role == ROLE_TERMINAL:
                    return ModelLaneWorkTerminal.model_validate(output.payload)
        return None

    def _status_reply(
        self, workflow_id: str, correlation_id: UUID
    ) -> ModelLaneWorkStepCompleted:
        record = self.newest(workflow_id)
        assert record is not None
        reply = self._reply_of(record)
        return reply.model_copy(update={"correlation_id": correlation_id})

    def _wait_until(self, record: ModelLaneWorkTransitionRecord) -> datetime:
        times = [_moment(value) for value in record.state.deadline_times.values()]
        if times:
            return min(times)
        return record.receipt.occurred_at + timedelta(
            seconds=self._overlay.default_wait_seconds
        )

    @staticmethod
    def _reply_output(
        reply: ModelLaneWorkStepCompleted, receipt_id: str
    ) -> ModelLaneWorkOutput:
        return ModelLaneWorkOutput(
            output_id=f"{receipt_id}-reply-{reply.correlation_id.hex[:12]}",
            role=ROLE_STEP_COMPLETED,
            payload=reply.model_dump(mode="json"),
        )

    # ------------------------------------------------------------ publishing

    def _publish_new(
        self, outputs: tuple[ModelLaneWorkOutput, ...]
    ) -> tuple[ModelLaneWorkOutput, ...]:
        """Publish each output not yet published; ``published`` grows only after a publish returns."""
        sent: list[ModelLaneWorkOutput] = []
        for output in outputs:
            key = (output.role, output.output_id)
            if key in self._published:
                continue
            self.journal.publish(output.role, output.output_id, output.payload)
            self._published.add(key)
            sent.append(output)
        return tuple(sent)

    def _ensure_outputs(
        self, record: ModelLaneWorkTransitionRecord
    ) -> tuple[ModelLaneWorkOutput, ...]:
        """The receipt first (the commit point), then every output it lists, each once."""
        receipt_output = ModelLaneWorkOutput(
            output_id=record.receipt.receipt_id,
            role=ROLE_TRANSITION,
            payload=record.model_dump(mode="json"),
        )
        return self._publish_new((receipt_output, *record.outputs))

    # -------------------------------------------------------------- recovery

    def recover(self) -> None:
        """Rebuild every workflow from the transition topic, then publish what is missing.

        Before any live input folds: read each role back to learn the output ids already
        published, rebuild the state from the receipts, publish each listed output that
        is not on its topic. A command already on the broker is not sent again.
        """
        self._published = {
            (role, output_id)
            for role in (
                ROLE_TRANSITION,
                ROLE_STEP_COMPLETED,
                ROLE_TERMINAL,
                ROLE_LEDGER_APPEND,
                ROLE_LEDGER_QUERY,
                ROLE_LAB_PROOF,
                ROLE_REPO_WRITE,
                ROLE_HANDOFF,
            )
            for output_id, _ in self.journal.read(role)
        }
        history: dict[str, list[ModelLaneWorkTransitionRecord]] = {}
        for _, payload in self.journal.read(ROLE_TRANSITION):
            record = ModelLaneWorkTransitionRecord.model_validate(payload)
            history.setdefault(record.state.workflow_id, []).append(record)
        for records in history.values():
            records.sort(key=lambda record: record.receipt.seq)
        self._history = history
        for records in history.values():
            for record in records:
                self._ensure_outputs(record)


__all__: list[str] = ["HandlerLaneWorkOrchestrator", "correlation_of_action"]

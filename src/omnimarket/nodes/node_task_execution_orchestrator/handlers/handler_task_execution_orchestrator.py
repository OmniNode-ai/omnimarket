# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerTaskExecutionOrchestrator — generic ``task.execute`` route planner.

First vertical slice (OMN-12702). This handler COMPOSES existing authorities;
it must NOT become a new authority:

- It normalizes a raw prompt OR a fully formed ``ModelTaskContract`` into one
  ``ModelTaskContract`` (reused verbatim from omnibase_core — never duplicated).
- It deterministically maps each requirement and each mechanical DoD check to an
  existing route NAME (``delegation`` / ``verification``) WITHOUT executing it.
- When ``execute_mechanical_checks`` is set, it dispatches the contract's
  mechanical DoD checks to ``node_verification_receipt_generator`` (the execution
  authority) and aggregates the returned receipt UNCHANGED. task.execute PLANS
  checks; the verification node EXECUTES them. Terminal status is derived from
  the receipt's ``overall_pass`` — never re-decided here (OMN-12703).
- When ``execute_delegation`` is set (non-dry-run), it dispatches each
  requirement (coding/refactor/review work) through ``node_delegate_skill_
  orchestrator`` (the delegation route authority) and aggregates the route's
  typed responses UNCHANGED. The delegation route owns model/backend selection,
  the quality gate, the correlation id, and the output content; task.execute
  never reinterprets delegation success — a failed/timeout response stays a typed
  failure owned by the route (OMN-12704). Delegation is async, so it is only
  available on the ``handle_async`` / bus surfaces.
- Otherwise (the V1 default) it returns the route plan with NO side effects.
- An unsupported action produces a typed, deterministic failure — never a silent
  skip and never a free-text summary.

Two surfaces:

``handle(request) -> ModelTaskExecutionResult``
    Direct, in-process planning used by unit tests and the CLI entrypoint.

``process(value) -> bytes``
    Pattern-B bus consumer. Deserializes a ``ModelDispatchBusCommand`` from the
    command topic, plans, and returns a ``ModelDispatchBusTerminalResult`` JSON
    payload keyed on the originating ``correlation_id``. When wired to an event
    bus, the terminal result is also published to the command's ``response_topic``.

Route map (documented for DoD): see module ``__doc__`` and ``contract.yaml``.
    owner node:        node_task_execution_orchestrator
    command topic:     onex.cmd.omnimarket.task-execute-start.v1
    input model:       ModelDispatchBusCommand (Pattern-B) -> ModelTaskExecutionRequest
    side-effect class: read_only (planner only; no side effects in V1)
    terminal events:   onex.evt.omnimarket.task-execute-completed.v1 (ok)
                       onex.evt.omnimarket.task-execute-failed.v1 (unsupported/invalid)
    evidence type:     ModelTaskExecutionResult (normalized contract + route plan)

Unsupported actions (typed deterministic failure, never silent):
    - request with neither prompt nor task_contract.
    - request with both prompt and task_contract.
    - non-dry-run request (V1 performs no side effects).
    - a DoD check whose check_type has no mapped route.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Literal, Protocol

from omnibase_core.enums.enum_check_type import EnumCheckType
from omnibase_core.models.dispatch.model_dispatch_bus_command import (
    ModelDispatchBusCommand,
)
from omnibase_core.models.dispatch.model_dispatch_bus_terminal_result import (
    ModelDispatchBusTerminalResult,
)
from omnibase_core.models.task.model_mechanical_check import ModelMechanicalCheck
from omnibase_core.models.task.model_task_contract import ModelTaskContract

from omnimarket.events.verification import (
    ModelVerificationReceipt,
    ModelVerificationReceiptRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_task_execution_orchestrator.models.model_task_execution import (
    EnumRouteItemKind,
    EnumTaskRoute,
    ModelRouteDecision,
    ModelTaskExecutionRequest,
    ModelTaskExecutionResult,
)
from omnimarket.nodes.node_verification_receipt_generator.handlers.handler_verification_receipt import (
    HandlerVerificationReceiptGenerator,
)

_log = logging.getLogger(__name__)

# Deterministic mechanical-check -> route mapping. Every EnumCheckType value is
# present so an added enum member without a mapping fails the runtime guard in
# ``_plan_check`` (``_CHECK_TYPE_ROUTE.get(...) is None -> raise``) rather than
# silently skipping. This is a runtime check, not a compile-time exhaustiveness
# guarantee.
_CHECK_TYPE_ROUTE: dict[EnumCheckType, EnumTaskRoute] = {
    EnumCheckType.COMMAND_EXIT_0: EnumTaskRoute.VERIFICATION,
    EnumCheckType.FILE_EXISTS: EnumTaskRoute.VERIFICATION,
    EnumCheckType.GREP_ABSENT: EnumTaskRoute.VERIFICATION,
    EnumCheckType.GREP_PRESENT: EnumTaskRoute.VERIFICATION,
}

_DelegateTaskType = Literal[
    "test",
    "document",
    "research",
    "code_generation",
    "code_review",
    "refactor",
    "reasoning",
    "complex_reasoning",
    "planning",
    "review",
    "summarization",
    "agent_delegation",
    "escalation",
]
_DelegateSource = Literal["claude-code", "codex"]


class UnsupportedTaskActionError(Exception):
    """Raised when a task element cannot be mapped to an existing route.

    Carries a deterministic ``reason`` so callers emit a typed failure rather
    than a free-text summary.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProtocolTaskExecutionPublisher(Protocol):
    """Minimal publish surface used to emit the Pattern-B terminal result."""

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
    ) -> None: ...


class ProtocolMechanicalCheckExecutor(Protocol):
    """Execution authority for mechanical DoD checks.

    Implemented by node_verification_receipt_generator's handler. task.execute
    PLANS checks then dispatches them through this port; it never executes a
    check itself and never transforms the returned receipt.
    """

    def handle(
        self, request: ModelVerificationReceiptRequest
    ) -> ModelVerificationReceipt: ...


class ProtocolDelegationExecutor(Protocol):
    """Execution authority for coding/refactor/review delegation.

    Implemented by node_delegate_skill_orchestrator's handler. task.execute
    PLANS each requirement onto the delegation route then dispatches it through
    this port; it never selects a model/backend, never enforces a quality gate,
    and never transforms the returned response. The delegation route owns
    success/failure — a failed/timeout response is a typed failure owned there,
    not reinterpreted here.
    """

    async def handle(
        self, request: ModelDelegateSkillRequest
    ) -> ModelDelegateSkillResponse: ...


def _fingerprint(contract: ModelTaskContract) -> str:
    """Deterministic SHA-256 over requirements + DoD checks (not timestamps).

    Identity is the planned work, not when the contract was generated, so the
    fingerprint and route plan stay stable across invocations.
    """
    material = {
        "requirements": list(contract.requirements),
        "definition_of_done": [
            {
                "criterion": c.criterion,
                "check": c.check,
                "check_type": c.check_type.value,
            }
            for c in contract.definition_of_done
        ],
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _mechanical_failure_reason(receipt: ModelVerificationReceipt) -> str:
    """Deterministic failure reason naming each failed verification dimension.

    Read directly off the receipt's check evidence (never recomputed), so the
    reason is a faithful, additive summary of the verification authority's own
    pass/fail — task.execute does not reinterpret outcomes.
    """
    failed = [c.dimension for c in receipt.checks if not c.passed]
    return "mechanical checks failed: " + ", ".join(failed)


def _delegation_failure_reason(
    responses: tuple[ModelDelegateSkillResponse, ...],
) -> str:
    """Deterministic failure reason naming each non-completed delegation.

    Read directly off each response's own ``status`` (never recomputed), so the
    reason faithfully reflects the delegation route's own success/failure —
    task.execute does not reinterpret delegation outcomes.
    """
    failed = [
        f"{r.task_type}:{r.status}:{r.correlation_id}"
        for r in responses
        if r.status != "completed"
    ]
    return "delegation failed: " + ", ".join(failed)


def _combine_failure_reasons(*reasons: str | None) -> str | None:
    """Additively aggregate typed failure reasons without dropping dimensions."""
    present = [reason for reason in reasons if reason]
    if not present:
        return None
    return "; ".join(present)


def _classify_delegate_task_type(requirement: str) -> _DelegateTaskType:
    """Map requirement text to the delegation route's allowed task taxonomy."""
    text = requirement.lower()
    if any(
        token in text
        for token in (
            "code review",
            "review code",
            "review the code",
            "audit",
            "security review",
        )
    ):
        return "code_review"
    if any(
        token in text
        for token in ("refactor", "restructure", "clean up", "cleanup", "simplify")
    ):
        return "refactor"
    if any(
        token in text
        for token in ("test", "pytest", "unit test", "integration test", "coverage")
    ):
        return "test"
    if any(
        token in text
        for token in ("document", "documentation", "readme", "runbook", "changelog")
    ):
        return "document"
    if any(token in text for token in ("research", "investigate", "spike")):
        return "research"
    if any(token in text for token in ("plan", "design", "proposal")):
        return "planning"
    if any(token in text for token in ("summarize", "summary")):
        return "summarization"
    if "review" in text:
        return "review"
    return "code_generation"


def _delegate_source(contract: ModelTaskContract) -> _DelegateSource:
    """Derive a registered delegation adapter source from task provenance."""
    generated_by = contract.generated_by.lower().replace("_", "-")
    if "claude" in generated_by:
        return "claude-code"
    return "codex"


def _delegate_metadata(
    contract: ModelTaskContract,
    requirement_index: int,
) -> dict[str, str]:
    """Stable provenance metadata for the delegation route request."""
    metadata = {
        "task_contract_id": contract.task_id,
        "requirement_index": str(requirement_index),
        "generated_by": contract.generated_by,
    }
    if contract.parent_ticket:
        metadata["parent_ticket"] = contract.parent_ticket
    if contract.repo:
        metadata["repo"] = contract.repo
    if contract.branch:
        metadata["branch"] = contract.branch
    return metadata


class HandlerTaskExecutionOrchestrator:
    """Generic ``task.execute`` route planner (V1: deterministic, no side effects)."""

    def __init__(
        self,
        event_bus: ProtocolTaskExecutionPublisher | None = None,
        mechanical_check_executor: ProtocolMechanicalCheckExecutor | None = None,
        delegation_executor: ProtocolDelegationExecutor | None = None,
    ) -> None:
        # event_bus is required for the Pattern-B publish path; the test-only
        # in-process path (handle / process-return) works without it.
        self._event_bus = event_bus
        # The verification node is the execution authority for mechanical checks.
        # Injected for tests; defaults to the real handler so the runtime path
        # composes the canonical authority rather than reimplementing it.
        self._mechanical_check_executor = mechanical_check_executor
        # node_delegate_skill_orchestrator is the execution authority for
        # coding/refactor/review delegation. Injected (stubbed) for tests;
        # defaults to the real handler so the runtime path composes the canonical
        # delegation route rather than routing around base dispatch.
        self._delegation_executor = delegation_executor

    def _get_mechanical_check_executor(self) -> ProtocolMechanicalCheckExecutor:
        if self._mechanical_check_executor is not None:
            return self._mechanical_check_executor
        return HandlerVerificationReceiptGenerator()

    def _get_delegation_executor(self) -> ProtocolDelegationExecutor:
        if self._delegation_executor is not None:
            return self._delegation_executor
        from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
            HandlerDelegateSkill,
        )

        # Defaults to the canonical delegation handler, which resolves its own
        # runtime dispatch port. The runtime DI container injects a bus-wired
        # delegation_executor; task.execute never selects transport itself.
        return HandlerDelegateSkill()

    # ------------------------------------------------------------------
    # Direct in-process surface
    # ------------------------------------------------------------------
    def handle(
        self,
        request: ModelTaskExecutionRequest,
        *,
        generated_at: datetime,
    ) -> ModelTaskExecutionResult:
        """Normalize input into a contract and return a deterministic route plan.

        ``generated_at`` is the timestamp stamped onto a contract normalized from
        a raw prompt. The caller supplies it from the originating command's
        ``created_at`` so identical input envelopes produce byte-identical
        terminal payloads (true idempotency); it is unused when a fully formed
        ``task_contract`` is supplied (that contract is reused verbatim).

        Raises UnsupportedTaskActionError for unsupported actions so the caller
        (CLI / bus consumer) decides how to surface the typed failure. Delegation
        execution requires the async surface (``handle_async``); requesting it
        here is a typed failure rather than a silent skip.
        """
        if request.execute_delegation:
            raise UnsupportedTaskActionError(
                "execute_delegation requires the async surface; call handle_async "
                "(or dispatch via the bus) so the delegation route can be awaited."
            )
        return self._plan_and_verify(request, generated_at=generated_at)

    async def handle_async(
        self,
        request: ModelTaskExecutionRequest,
        *,
        generated_at: datetime,
    ) -> ModelTaskExecutionResult:
        """Async surface: planning + mechanical checks + coding delegation.

        Delegates each requirement (coding/refactor/review work) to the existing
        delegation route when ``execute_delegation`` is set, aggregating the
        route's typed responses UNCHANGED. The non-delegation flow is identical
        to ``handle`` so dry-run planning and mechanical checks behave the same on
        both surfaces.
        """
        base = self._plan_and_verify(request, generated_at=generated_at)
        if not request.execute_delegation:
            return base
        return await self._execute_delegation(request, base)

    def _plan_and_verify(
        self,
        request: ModelTaskExecutionRequest,
        *,
        generated_at: datetime,
    ) -> ModelTaskExecutionResult:
        """Shared sync flow: validate, normalize, plan, optionally verify checks."""
        self._validate_supported(request)
        contract = self._normalize_contract(request, generated_at=generated_at)
        route_plan = self._plan_routes(contract)

        if not request.execute_mechanical_checks:
            return ModelTaskExecutionResult(
                ok=True,
                dry_run=request.dry_run,
                task_contract=contract,
                contract_fingerprint=_fingerprint(contract),
                route_plan=route_plan,
            )

        receipt = self._execute_mechanical_checks(contract, request.worktree_path)
        # Evidence aggregation is additive only: the receipt is stored verbatim
        # and the terminal status is derived from its overall_pass — task.execute
        # never re-decides a check outcome. A failed check yields a deterministic
        # failure_reason naming the failed dimensions.
        failure_reason = (
            None if receipt.overall_pass else _mechanical_failure_reason(receipt)
        )
        return ModelTaskExecutionResult(
            ok=receipt.overall_pass,
            dry_run=request.dry_run,
            task_contract=contract,
            contract_fingerprint=_fingerprint(contract),
            route_plan=route_plan,
            verification_receipt=receipt,
            failure_reason=failure_reason,
        )

    async def _execute_delegation(
        self,
        request: ModelTaskExecutionRequest,
        base: ModelTaskExecutionResult,
    ) -> ModelTaskExecutionResult:
        """Dispatch each requirement through the delegation route, aggregate verbatim.

        Each requirement maps to one ``ModelDelegateSkillRequest`` dispatched to
        node_delegate_skill_orchestrator. The route owns model/backend selection,
        the quality gate, the correlation id, and the output content; task.execute
        stores each typed response UNCHANGED (additive) and never reinterprets
        success. A non-completed response keeps ``base.ok`` semantics intact and
        contributes a deterministic ``failure_reason`` read off the route's own
        status.
        """
        executor = self._get_delegation_executor()
        responses: list[ModelDelegateSkillResponse] = []
        source = _delegate_source(base.task_contract)
        for index, requirement in enumerate(base.task_contract.requirements):
            delegate_request = ModelDelegateSkillRequest(
                prompt=requirement,
                task_type=_classify_delegate_task_type(requirement),
                source=source,
                wait=True,
                metadata=_delegate_metadata(base.task_contract, index),
            )
            responses.append(await executor.handle(delegate_request))

        delegation_responses = tuple(responses)
        all_completed = all(r.status == "completed" for r in delegation_responses)
        # task.execute does NOT reinterpret delegation success: ``ok`` stays true
        # only when the prior flow passed AND every delegation reported completed
        # by its own status. A failed/timeout response is a typed failure owned by
        # the delegation route; we surface it, never re-decide it.
        failure_reason = _combine_failure_reasons(
            base.failure_reason,
            None if all_completed else _delegation_failure_reason(delegation_responses),
        )

        return base.model_copy(
            update={
                "ok": base.ok and all_completed,
                "delegation_responses": delegation_responses,
                "failure_reason": failure_reason,
            }
        )

    def _execute_mechanical_checks(
        self, contract: ModelTaskContract, worktree_path: str
    ) -> ModelVerificationReceipt:
        """Dispatch the contract's mechanical checks to the verification node.

        task.execute composes the verification authority: it builds the request
        from the contract's DoD (unchanged) and returns whatever receipt the
        node produces. CI and pytest dimensions are disabled here — only the
        contract-declared mechanical checks are executed.
        """
        executor = self._get_mechanical_check_executor()
        verification_request = ModelVerificationReceiptRequest(
            task_id=contract.task_id,
            claim="task.execute mechanical DoD verification",
            worktree_path=worktree_path,
            verify_ci=False,
            verify_tests=False,
            mechanical_checks=tuple(contract.definition_of_done),
        )
        return executor.handle(verification_request)

    # ------------------------------------------------------------------
    # Pattern-B bus consumer surface
    # ------------------------------------------------------------------
    async def process(self, value: bytes) -> bytes:
        """Consume a ModelDispatchBusCommand and return a terminal result.

        Always returns a ModelDispatchBusTerminalResult JSON payload keyed on the
        originating correlation_id. When an event bus is wired, the same terminal
        result is also published to the command's response_topic.
        """
        command = ModelDispatchBusCommand.model_validate_json(value)
        terminal = await self._terminal_from_command(command)

        if self._event_bus is not None:
            await self._event_bus.publish(
                topic=command.response_topic,
                key=str(command.correlation_id).encode("utf-8"),
                value=terminal.model_dump_json().encode("utf-8"),
            )
        return terminal.model_dump_json().encode("utf-8")

    async def _terminal_from_command(
        self,
        command: ModelDispatchBusCommand,
    ) -> ModelDispatchBusTerminalResult:
        """Plan the command payload and reduce it to a Pattern-B terminal result.

        Uses the async surface so a delegation request dispatched over the bus is
        awaited through the delegation route.
        """
        try:
            request = self._request_from_command(command)
            result = await self.handle_async(request, generated_at=command.created_at)
        except UnsupportedTaskActionError as exc:
            return ModelDispatchBusTerminalResult(
                correlation_id=command.correlation_id,
                status="failed",
                payload=None,
                error_message=exc.reason,
            )
        return ModelDispatchBusTerminalResult(
            correlation_id=command.correlation_id,
            status="completed",
            payload=result.model_dump(mode="json"),
            error_message=None,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _request_from_command(
        self, command: ModelDispatchBusCommand
    ) -> ModelTaskExecutionRequest:
        """Build the route boundary request from a Pattern-B command payload."""
        payload = command.payload
        if not isinstance(payload, dict):
            raise UnsupportedTaskActionError(
                "task.execute payload must be a JSON object with prompt or task_contract."
            )
        return ModelTaskExecutionRequest.model_validate(payload)

    def _validate_supported(self, request: ModelTaskExecutionRequest) -> None:
        """Reject unsupported actions with a typed, deterministic reason."""
        has_prompt = request.prompt is not None
        has_contract = request.task_contract is not None
        if has_prompt == has_contract:
            raise UnsupportedTaskActionError(
                "exactly one of prompt or task_contract must be supplied."
            )
        # Delegation is the one supported non-dry-run side effect: the work is
        # owned and executed by the delegation route, not by task.execute. Every
        # other non-dry-run action remains unsupported (no PR/branch side effects
        # in this slice).
        if not request.dry_run and not request.execute_delegation:
            raise UnsupportedTaskActionError(
                "non-dry-run task.execute is only supported for execute_delegation; "
                "no other side effects are performed in this slice."
            )
        if request.dry_run and request.execute_delegation:
            raise UnsupportedTaskActionError(
                "execute_delegation is a real side effect and requires dry_run=False."
            )
        if request.allowed_side_effects:
            raise UnsupportedTaskActionError(
                "allowed_side_effects must be empty in the first vertical slice; "
                "V1 performs no side effects."
            )

    def _normalize_contract(
        self,
        request: ModelTaskExecutionRequest,
        *,
        generated_at: datetime,
    ) -> ModelTaskContract:
        """Return the supplied contract verbatim or build one from the prompt.

        A prompt-normalized contract derives ``generated_at`` from the caller's
        timestamp (the originating command's ``created_at``) rather than
        wall-clock, so identical input produces an identical contract — keeping
        the contract's ``idempotent: true`` claim honest and replay byte-stable.
        """
        if request.task_contract is not None:
            return request.task_contract

        # request.prompt is guaranteed non-None by _validate_supported.
        prompt = request.prompt
        assert prompt is not None
        task_id = "task-" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        return ModelTaskContract(
            task_id=task_id,
            parent_ticket=request.ticket_id,
            repo=request.target_repo,
            generated_at=generated_at,
            generated_by="node_task_execution_orchestrator",
            requirements=[prompt],
        )

    def _plan_routes(
        self, contract: ModelTaskContract
    ) -> tuple[ModelRouteDecision, ...]:
        """Deterministic route plan: requirements then DoD checks, in order.

        Requirements are coding/refactor/review work -> delegation route.
        Mechanical DoD checks -> verification route (per _CHECK_TYPE_ROUTE).
        An unmapped check_type raises a typed failure (no silent skip).
        """
        decisions: list[ModelRouteDecision] = []

        for requirement in contract.requirements:
            decisions.append(
                ModelRouteDecision(
                    kind=EnumRouteItemKind.REQUIREMENT,
                    source=requirement,
                    route=EnumTaskRoute.DELEGATION,
                    detail="requirement -> node_delegate_skill_orchestrator",
                )
            )

        for check in contract.definition_of_done:
            decisions.append(self._plan_check(check))

        return tuple(decisions)

    def _plan_check(self, check: ModelMechanicalCheck) -> ModelRouteDecision:
        """Map one mechanical check to the verification route deterministically."""
        route = _CHECK_TYPE_ROUTE.get(check.check_type)
        if route is None:
            raise UnsupportedTaskActionError(
                f"unsupported mechanical check_type {check.check_type.value!r}; "
                "no route mapping exists."
            )
        return ModelRouteDecision(
            kind=EnumRouteItemKind.MECHANICAL_CHECK,
            source=check.criterion,
            route=route,
            detail=(f"{check.check_type.value} -> node_verification_receipt_generator"),
        )


__all__ = [
    "HandlerTaskExecutionOrchestrator",
    "ProtocolDelegationExecutor",
    "ProtocolMechanicalCheckExecutor",
    "ProtocolTaskExecutionPublisher",
    "UnsupportedTaskActionError",
]

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
from typing import Protocol

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


class HandlerTaskExecutionOrchestrator:
    """Generic ``task.execute`` route planner (V1: deterministic, no side effects)."""

    def __init__(
        self,
        event_bus: ProtocolTaskExecutionPublisher | None = None,
        mechanical_check_executor: ProtocolMechanicalCheckExecutor | None = None,
    ) -> None:
        # event_bus is required for the Pattern-B publish path; the test-only
        # in-process path (handle / process-return) works without it.
        self._event_bus = event_bus
        # The verification node is the execution authority for mechanical checks.
        # Injected for tests; defaults to the real handler so the runtime path
        # composes the canonical authority rather than reimplementing it.
        self._mechanical_check_executor = mechanical_check_executor

    def _get_mechanical_check_executor(self) -> ProtocolMechanicalCheckExecutor:
        if self._mechanical_check_executor is not None:
            return self._mechanical_check_executor
        return HandlerVerificationReceiptGenerator()

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
        (CLI / bus consumer) decides how to surface the typed failure.
        """
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
        terminal = self._terminal_from_command(command)

        if self._event_bus is not None:
            await self._event_bus.publish(
                topic=command.response_topic,
                key=str(command.correlation_id).encode("utf-8"),
                value=terminal.model_dump_json().encode("utf-8"),
            )
        return terminal.model_dump_json().encode("utf-8")

    def _terminal_from_command(
        self,
        command: ModelDispatchBusCommand,
    ) -> ModelDispatchBusTerminalResult:
        """Plan the command payload and reduce it to a Pattern-B terminal result."""
        try:
            request = self._request_from_command(command)
            result = self.handle(request, generated_at=command.created_at)
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
        if not request.dry_run:
            raise UnsupportedTaskActionError(
                "non-dry-run task.execute is unsupported in the first vertical slice; "
                "V1 performs no side effects."
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
    "ProtocolMechanicalCheckExecutor",
    "ProtocolTaskExecutionPublisher",
    "UnsupportedTaskActionError",
]

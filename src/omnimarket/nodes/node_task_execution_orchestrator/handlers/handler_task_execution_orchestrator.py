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
- When ``create_pr`` is set, it runs the PR creation path (OMN-12705),
  NON-BLOCKING for V1 and fully isolated/optional. node_ticket_work cannot be
  invoked cleanly here (it requires a Linear ticket id and drives a 7-phase FSM
  keyed on ``ModelTicketWorkCommand``; task.execute only holds a normalized
  ``ModelTaskContract``), so per the DoD escape hatch a narrow create_pr port is
  added: ``ProtocolPrCreationExecutor`` (structurally the git client's
  ``create_pr`` behind node_ticket_work). Dry-run returns the intended
  branch/title/body (``ModelPrPlan``) with NO side effects. Non-dry-run is gated
  behind ``dry_run=False`` + ``'create_pr'`` in ``allowed_side_effects`` + an
  injected executor, then aggregates the returned ``ModelRunResult`` UNCHANGED.
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
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.nodes.node_task_execution_orchestrator.models.model_task_execution import (
    SIDE_EFFECT_CREATE_PR,
    EnumRouteItemKind,
    EnumTaskRoute,
    ModelPrPlan,
    ModelRouteDecision,
    ModelTaskExecutionRequest,
    ModelTaskExecutionResult,
)
from omnimarket.nodes.node_ticket_work.protocols.protocol_git_client import (
    ModelRunResult,
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


class ProtocolPrCreationExecutor(Protocol):
    """Execution authority for PR creation.

    Structurally compatible with ``ProtocolGitClient.create_pr`` (node_ticket_work),
    so the real subprocess-backed git client is injected verbatim at runtime.
    task.execute PLANS the PR (branch/title/body) then dispatches it through this
    port; it never runs ``gh`` itself and never transforms the returned result.
    The create_pr authority owns the PR URL and any failure — a failed result is
    a typed failure owned there, not reinterpreted here. NON-BLOCKING for V1.
    """

    def create_pr(
        self,
        worktree_path: str,
        title: str,
        body: str,
    ) -> ModelRunResult: ...


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


def _pr_failure_reason(result: ModelRunResult) -> str:
    """Deterministic failure reason read off the create_pr authority's result.

    Reads the authority's own exit code and stderr (never recomputed), so the
    reason faithfully reflects the create_pr authority's own outcome —
    task.execute does not reinterpret PR creation success/failure.
    """
    return f"pr creation failed: exit_code={result.exit_code} {result.stderr.strip()}"


def _build_pr_body(contract: ModelTaskContract) -> str:
    """Deterministic PR body summarizing the contract's requirements and DoD.

    Read straight off the normalized contract (never wall-clock or external
    state) so the same contract yields a byte-stable body — keeping the dry-run
    plan replay-stable. task.execute plans the PR text; it does not own the PR.
    """
    lines: list[str] = []
    if contract.parent_ticket is not None:
        lines.append(f"Ticket: {contract.parent_ticket}")
    lines.append(f"Contract fingerprint: {_fingerprint(contract)}")
    lines.append("")
    lines.append("## Requirements")
    for requirement in contract.requirements:
        lines.append(f"- {requirement}")
    lines.append("")
    lines.append("## Definition of Done")
    for check in contract.definition_of_done:
        lines.append(f"- [{check.check_type.value}] {check.criterion}: `{check.check}`")
    return "\n".join(lines)


class HandlerTaskExecutionOrchestrator:
    """Generic ``task.execute`` route planner (V1: deterministic, no side effects)."""

    def __init__(
        self,
        event_bus: ProtocolTaskExecutionPublisher | None = None,
        mechanical_check_executor: ProtocolMechanicalCheckExecutor | None = None,
        delegation_executor: ProtocolDelegationExecutor | None = None,
        pr_creation_executor: ProtocolPrCreationExecutor | None = None,
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
        # The git client behind node_ticket_work is the execution authority for
        # PR creation. There is NO default here: non-dry-run create_pr requires an
        # explicitly injected executor (the runtime DI container wires the real
        # subprocess-backed git client). Dry-run create_pr needs no executor — it
        # only plans branch/title/body. This keeps PR creation NON-BLOCKING for V1
        # (a missing executor is a typed failure, never an implicit side effect).
        self._pr_creation_executor = pr_creation_executor

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
        base = self._plan_and_verify(request, generated_at=generated_at)
        return self._apply_pr_path(request, base)

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
        if request.execute_delegation:
            base = await self._execute_delegation(request, base)
        return self._apply_pr_path(request, base)

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
        for requirement in base.task_contract.requirements:
            delegate_request = ModelDelegateSkillRequest(
                prompt=requirement,
                task_type="code_generation",
                source="claude-code",
                wait=True,
            )
            responses.append(await executor.handle(delegate_request))

        delegation_responses = tuple(responses)
        all_completed = all(r.status == "completed" for r in delegation_responses)
        # task.execute does NOT reinterpret delegation success: ``ok`` stays true
        # only when the prior flow passed AND every delegation reported completed
        # by its own status. A failed/timeout response is a typed failure owned by
        # the delegation route; we surface it, never re-decide it.
        failure_reason = base.failure_reason
        if failure_reason is None and not all_completed:
            failure_reason = _delegation_failure_reason(delegation_responses)

        return base.model_copy(
            update={
                "ok": base.ok and all_completed,
                "delegation_responses": delegation_responses,
                "failure_reason": failure_reason,
            }
        )

    # ------------------------------------------------------------------
    # PR creation path (NON-BLOCKING V1; isolated/optional)
    # ------------------------------------------------------------------
    def _apply_pr_path(
        self,
        request: ModelTaskExecutionRequest,
        base: ModelTaskExecutionResult,
    ) -> ModelTaskExecutionResult:
        """Plan (always) and, on non-dry-run, execute the PR creation path.

        This is additive and isolated: when ``create_pr`` is not requested the
        base result is returned unchanged. The dry-run plan is pure (intended
        branch/title/body, no side effects). The non-dry-run path COMPOSES the PR
        creation authority (git client) — already validated as injected + allowed
        in ``_validate_supported`` — and aggregates its ``ModelRunResult``
        UNCHANGED. task.execute never reinterprets the PR outcome; a failed result
        contributes a deterministic ``failure_reason`` read off the authority's
        own exit code/stderr.
        """
        if not request.create_pr:
            return base

        pr_plan = self._build_pr_plan(base.task_contract, request.worktree_path)
        if request.dry_run:
            # Dry-run: return the intended PR identity with NO side effects.
            return base.model_copy(update={"pr_plan": pr_plan})

        # Non-dry-run: executor presence + allowance were enforced in
        # _validate_supported; assert narrows the type for mypy without a default.
        executor = self._pr_creation_executor
        assert executor is not None
        pr_result = executor.create_pr(
            worktree_path=pr_plan.worktree_path,
            title=pr_plan.title,
            body=pr_plan.body,
        )
        failure_reason = base.failure_reason
        if failure_reason is None and not pr_result.success:
            failure_reason = _pr_failure_reason(pr_result)
        return base.model_copy(
            update={
                "ok": base.ok and pr_result.success,
                "pr_plan": pr_plan,
                "pr_result": pr_result,
                "failure_reason": failure_reason,
            }
        )

    def _build_pr_plan(
        self, contract: ModelTaskContract, worktree_path: str
    ) -> ModelPrPlan:
        """Derive a deterministic PR identity (branch/title/body) from the contract.

        Identity is read straight off the normalized contract so the same contract
        always yields the same plan (replay-stable). The branch is required for a
        PR; a contract without one is a typed failure rather than an invented
        default. The title/body summarize the contract's requirements and DoD —
        task.execute plans the PR text; the create_pr authority owns the push.
        """
        if not contract.branch:
            raise UnsupportedTaskActionError(
                "create_pr requires the task contract to declare a branch; "
                "task.execute never invents a branch name."
            )
        ticket = contract.parent_ticket or contract.task_id
        title = f"{ticket}: task.execute PR ({len(contract.requirements)} requirements)"
        body = _build_pr_body(contract)
        return ModelPrPlan(
            branch=contract.branch,
            title=title,
            body=body,
            worktree_path=worktree_path,
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
        # Delegation and create_pr are the supported non-dry-run side effects: the
        # work is owned and executed by the composed authority (delegation route /
        # git client), not by task.execute. Every other non-dry-run action remains
        # unsupported.
        if (
            not request.dry_run
            and not request.execute_delegation
            and not request.create_pr
        ):
            raise UnsupportedTaskActionError(
                "non-dry-run task.execute is only supported for execute_delegation "
                "or create_pr; no other side effects are performed in this slice."
            )
        if request.dry_run and request.execute_delegation:
            raise UnsupportedTaskActionError(
                "execute_delegation is a real side effect and requires dry_run=False."
            )
        # allowed_side_effects is constrained to the single PR-creation allowance
        # token; any other token remains unsupported in this slice.
        if set(request.allowed_side_effects) - {SIDE_EFFECT_CREATE_PR}:
            raise UnsupportedTaskActionError(
                "allowed_side_effects may only contain 'create_pr' in this slice; "
                "no other side effects are performed."
            )
        # Non-dry-run create_pr is a real side effect gated behind explicit
        # allowance AND an injected executor — never implicit (DoD: non-dry-run
        # gated behind explicit side-effect allowance + fixtures). Dry-run
        # create_pr only plans (no allowance / executor required).
        if request.create_pr and not request.dry_run:
            if SIDE_EFFECT_CREATE_PR not in request.allowed_side_effects:
                raise UnsupportedTaskActionError(
                    "non-dry-run create_pr requires 'create_pr' in "
                    "allowed_side_effects; PR creation is never implicit."
                )
            if self._pr_creation_executor is None:
                raise UnsupportedTaskActionError(
                    "non-dry-run create_pr requires an injected pr_creation_executor; "
                    "the create_pr path is NON-BLOCKING and never self-executes."
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
    "ProtocolPrCreationExecutor",
    "ProtocolTaskExecutionPublisher",
    "UnsupportedTaskActionError",
]

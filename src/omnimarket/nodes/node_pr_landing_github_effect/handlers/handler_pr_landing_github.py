# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler of node_pr_landing_github_effect (OMN-19831, plan task T9).

One typed command in, one typed completed or failed result out. Every GitHub
call goes through the shared landing transport (``omnimarket.github_landing``),
the same send path node_ci_rerun_effect, node_merge_sweep_auto_merge_arm_effect
and the fix effect's auto-rebase use.

Rules this handler enforces (knowledge-base#94, revision 1 of the plan):

- **dry_run** records the requests the command plans and sends nothing.
- **Quota from headers only.** Every response's ``x-ratelimit-*`` headers
  become the result's :class:`ModelGithubQuotaReading`; the ``rate_limit``
  endpoint is never read. A request whose resource's last reading is under the
  contract's floor, inside that reading's window, is refused with
  ``quota_floor`` before it is sent.
- **Arm and enqueue read first (P1, R4).** One GraphQL read returns the PR's
  head, draft flag, title and labels (the hold markers), open state and the
  repository's live merge policy. The mutation is sent only when the PR is
  open, at the expected head, not draft, not held, and the policy fits the
  operation (auto-merge allowed and no merge queue for an arm; a merge queue
  for an enqueue). The mutation also carries the expected head, so GitHub
  refuses it if the head moves between the read and the mutation.
- **Conditional reads.** read_head_checks and read_pr_state send the last
  ETag, so an unchanged PR answers 304 and costs no quota.
- **Run attempts (F7).** read_head_checks reads each failed copy's run
  attempt from its run's jobs list; rerun_runs reads back the attempt each
  re-run started.
- **Required contexts (OMN-20866).** read_head_checks asked with a base branch
  also reads that branch's required status contexts (classic protection and
  the rulesets in force), so the classifier judges only what blocks a merge.
- **An arm already in place holds (OMN-20866).** When the policy read shows
  auto-merge already armed at the expected head, the arm completes without
  sending the mutation again. An enqueue completes the same way when the PR
  is already in the merge queue or armed to join it.
- **A PR GitHub already reports mergeable is merged (OMN-20866).** GitHub
  refuses to arm auto-merge on a PR whose merge state is clean, unstable or
  has_hooks ("Pull request is in clean status"), so an arm whose policy read
  shows one of those, with no arm in place, sends mergePullRequest with the
  expected head instead. That is the only merge call in this module; it
  passes every refusal an arm passes first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from omnimarket.events.pr_landing_github.model_github_check_run_fact import (
    required_contexts_from_branch_body,
    required_contexts_from_rules_body,
)
from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    branch_request,
    branch_rules_request,
    head_check_runs_request,
    merge_at_head_request,
    run_jobs_request,
    workflow_run_request,
)
from omnimarket.github_landing.github_landing_transport import (
    GithubLandingTransportError,
    UrllibGithubLandingTransport,
)
from omnimarket.github_landing.model_github_http_exchange import (
    GITHUB_GRAPHQL_PATH,
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.merge_control.hold_marker import evaluate_merge_hold
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
    EnumPrLandingGithubMode,
    EnumPrLandingGithubOperation,
    GithubPrStateParseError,
    ModelGithubCheckRunFact,
    ModelGithubPrStateFact,
    ModelGithubQuotaFloor,
    ModelGithubQuotaHeadersMissingError,
    ModelGithubQuotaReading,
    ModelGithubRunAttempt,
    ModelPrLandingGithubCompleted,
    ModelPrLandingGithubFailed,
    ModelPrLandingGithubRequest,
    classify_github_response,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.model_github_check_run_fact import (
    job_attempts_from_jobs_body,
)
from omnimarket.nodes.node_pr_landing_github_effect.protocols import (
    ProtocolPrLandingGithubTransport,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_SECRET_NAME = "GITHUB_TOKEN"
# A runaway pagination guard: 10 pages of 100 is far past any real head.
_MAX_PAGES = 10
# GitHub merge states in which the PR merges at once, so GitHub refuses to arm
# auto-merge on it (the immediately mergeable states, OMN-20866).
_MERGEABLE_NOW = frozenset({"clean", "has_hooks", "unstable"})

Result = ModelPrLandingGithubCompleted | ModelPrLandingGithubFailed


class _RefusedError(Exception):
    """Internal: carries the typed failure out of a multi-request operation."""

    def __init__(self, failed: ModelPrLandingGithubFailed) -> None:
        super().__init__(failed.detail)
        self.failed = failed


def _resource_of(request: ModelGithubHttpRequest) -> str:
    return "graphql" if request.path == GITHUB_GRAPHQL_PATH else "core"


class _Exchange:
    """The requests one command sent, their statuses and the last quota reading."""

    def __init__(
        self,
        handler: HandlerPrLandingGithubEffect,
        command: ModelPrLandingGithubRequest,
        transport: ProtocolPrLandingGithubTransport,
    ) -> None:
        self._handler = handler
        self.command = command
        self._transport = transport
        self.sent: list[ModelGithubHttpRequest] = []
        self.statuses: list[int] = []
        self.quota: ModelGithubQuotaReading | None = None

    def failed(
        self,
        reason: EnumPrLandingGithubFailureReason,
        detail: str,
        *,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
        quota: ModelGithubQuotaReading | None = None,
        pr_state: ModelGithubPrStateFact | None = None,
    ) -> _RefusedError:
        command = self.command
        return _RefusedError(
            ModelPrLandingGithubFailed(
                correlation_id=command.correlation_id,
                operation=command.operation,
                mode=command.mode,
                repository=command.repository,
                pr_number=command.pr_number,
                head_sha=command.head_sha,
                reason=reason,
                detail=detail or reason.value,
                http_status=http_status,
                retry_after_seconds=retry_after_seconds,
                pr_state=pr_state,
                quota=quota if quota is not None else self.quota,
            )
        )

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        """Send one request; raise :class:`_RefusedError` with the typed failure."""
        resource = _resource_of(request)
        floor_reading = self._handler.floor_refusal(resource)
        if floor_reading is not None:
            raise self.failed(
                EnumPrLandingGithubFailureReason.QUOTA_FLOOR,
                (
                    f"{resource} remaining {floor_reading.remaining} is under the "
                    f"contract floor until {floor_reading.reset}; no call made"
                ),
                quota=floor_reading,
            )
        try:
            response = await self._transport.send(request)
        except (GithubLandingTransportError, OSError) as exc:
            raise self.failed(
                EnumPrLandingGithubFailureReason.TRANSPORT_ERROR, str(exc)
            ) from None
        self.sent.append(request)
        self.statuses.append(response.status)
        try:
            reading = ModelGithubQuotaReading.from_response_headers(
                response.headers, identity=self._handler.identity
            )
        except ModelGithubQuotaHeadersMissingError as exc:
            if self.quota is None:
                raise self.failed(
                    EnumPrLandingGithubFailureReason.TRANSPORT_ERROR,
                    f"HTTP {response.status} from {request.path}: {exc}",
                ) from None
        else:
            self.quota = reading
            self._handler.record_reading(reading)
        reason = classify_github_response(self.command.operation, response)
        if reason is not None:
            raise self.failed(
                reason,
                response.message(),
                http_status=response.status,
                retry_after_seconds=response.retry_after_seconds(),
            )
        return response

    def completed(
        self,
        *,
        not_modified: bool = False,
        etag: str | None = None,
        check_runs: tuple[ModelGithubCheckRunFact, ...] = (),
        required_contexts: tuple[str, ...] | None = None,
        pr_state: ModelGithubPrStateFact | None = None,
        started_attempts: tuple[ModelGithubRunAttempt, ...] = (),
    ) -> ModelPrLandingGithubCompleted:
        command = self.command
        return ModelPrLandingGithubCompleted(
            correlation_id=command.correlation_id,
            operation=command.operation,
            mode=command.mode,
            repository=command.repository,
            pr_number=command.pr_number,
            head_sha=command.head_sha,
            requests=tuple(self.sent),
            http_statuses=tuple(self.statuses),
            not_modified=not_modified,
            etag=etag,
            check_runs=check_runs,
            required_contexts=required_contexts,
            pr_state=pr_state,
            started_attempts=started_attempts,
            quota=self.quota,
        )


class HandlerPrLandingGithubEffect:
    """EFFECT: run one PR landing GitHub operation and report it typed.

    ``transport`` is injected in tests (the recorded fake). Without one, the
    handler builds the live urllib transport per call from the contract-declared
    ``GITHUB_TOKEN`` ref. Quota readings are kept per resource across calls, so
    the floor applies to the next call after a low reading.
    """

    def __init__(
        self,
        transport: ProtocolPrLandingGithubTransport | None = None,
        *,
        quota_floor: ModelGithubQuotaFloor | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._floor = quota_floor or ModelGithubQuotaFloor.from_contract(_CONTRACT_PATH)
        self._clock = clock
        self.identity = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
        self._readings: dict[str, ModelGithubQuotaReading] = {}

    # --- quota ----------------------------------------------------------------

    def record_reading(self, reading: ModelGithubQuotaReading) -> None:
        self._readings[reading.resource] = reading

    def floor_refusal(self, resource: str) -> ModelGithubQuotaReading | None:
        """The last reading for ``resource`` when it is under the floor, in its window."""
        reading = self._readings.get(resource)
        if reading is None or self._clock() >= reading.reset:
            return None
        if self._floor.refusal(reading) is None:
            return None
        return reading

    # --- entry point ----------------------------------------------------------

    async def handle(self, request: ModelPrLandingGithubRequest) -> Result:
        """Run the command; dry_run records its planned requests and sends nothing."""
        if request.mode is EnumPrLandingGithubMode.DRY_RUN:
            return ModelPrLandingGithubCompleted(
                correlation_id=request.correlation_id,
                operation=request.operation,
                mode=request.mode,
                repository=request.repository,
                pr_number=request.pr_number,
                head_sha=request.head_sha,
                requests=request.to_http_requests(),
                http_statuses=(),
                quota=None,
            )
        transport = self._transport or await self._live_transport()
        exchange = _Exchange(self, request, transport)
        try:
            result: Result = await self._run(exchange)
        except _RefusedError as refused:
            result = refused.failed
        _log.info(
            "pr-landing github %s %s#%s mode=%s -> %s",
            request.operation.value,
            request.repository,
            request.pr_number,
            request.mode.value,
            getattr(result, "reason", "completed"),
        )
        return result

    async def _live_transport(self) -> UrllibGithubLandingTransport:
        ref = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
        # env_var_fallback (OMN-14452): the lane resolver does not serve the
        # GitHub token from the store; the literal container env var does.
        secret = await resolve_api_key_async(ref, env_var_fallback=ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {ref!r} resolved to None; set it in the secret store"
            )
        return UrllibGithubLandingTransport(secret)

    async def _run(self, exchange: _Exchange) -> ModelPrLandingGithubCompleted:
        op = exchange.command.operation
        if op is EnumPrLandingGithubOperation.RERUN_RUNS:
            return await self._rerun_runs(exchange)
        if op is EnumPrLandingGithubOperation.READ_HEAD_CHECKS:
            return await self._read_head_checks(exchange)
        if op is EnumPrLandingGithubOperation.READ_PR_STATE:
            return await self._read_pr_state(exchange)
        if op in (
            EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
            EnumPrLandingGithubOperation.ENQUEUE,
        ):
            return await self._arm_or_enqueue(exchange)
        # update_branch and disarm: one planned request, no follow-up.
        for planned in exchange.command.to_http_requests():
            await exchange.send(planned)
        return exchange.completed()

    # --- operations -----------------------------------------------------------

    async def _rerun_runs(self, exchange: _Exchange) -> ModelPrLandingGithubCompleted:
        command = exchange.command
        for planned in command.to_http_requests():
            await exchange.send(planned)
        started: list[ModelGithubRunAttempt] = []
        for run_id in command.run_ids:
            attempt: int | None = None
            try:
                response = await exchange.send(
                    workflow_run_request(command.repository, run_id)
                )
            except _RefusedError as refused:
                # The re-run was accepted; only the read-back failed.
                _log.warning(
                    "re-run read-back failed for run %s: %s",
                    run_id,
                    refused.failed.detail,
                )
            else:
                value = (response.body or {}).get("run_attempt")
                attempt = value if isinstance(value, int) and value >= 1 else None
            started.append(ModelGithubRunAttempt(run_id=run_id, run_attempt=attempt))
        return exchange.completed(started_attempts=tuple(started))

    async def _read_pr_state(
        self, exchange: _Exchange
    ) -> ModelPrLandingGithubCompleted:
        command = exchange.command
        (planned,) = command.to_http_requests()
        response = await exchange.send(planned)
        etag = response.header("etag") or command.etag
        if response.status == 304:
            return exchange.completed(not_modified=True, etag=etag)
        try:
            state = ModelGithubPrStateFact.from_rest_pull(response.body)
        except (GithubPrStateParseError, ValueError) as exc:
            raise exchange.failed(
                EnumPrLandingGithubFailureReason.VALIDATION_FAILED,
                f"unreadable pull request response: {exc}",
                http_status=response.status,
            ) from None
        return exchange.completed(etag=etag, pr_state=state)

    async def _read_head_checks(
        self, exchange: _Exchange
    ) -> ModelPrLandingGithubCompleted:
        command = exchange.command
        head_sha = command.required_head_sha()
        (first,) = command.to_http_requests()
        response = await exchange.send(first)
        etag = response.header("etag") or command.etag
        if response.status == 304:
            return exchange.completed(not_modified=True, etag=etag)
        facts = list(self._check_runs(exchange, response))
        page_len = len(facts)
        page = 1
        while page_len == LIST_PAGE_SIZE and page < _MAX_PAGES:
            page += 1
            more = await exchange.send(
                head_check_runs_request(command.repository, head_sha, page=page)
            )
            page_facts = self._check_runs(exchange, more)
            facts.extend(page_facts)
            page_len = len(page_facts)
        attempts: dict[int, int] = {}
        failed_runs = sorted({f.run_id for f in facts if f.failed and f.run_id})
        for run_id in failed_runs:
            attempts.update(await self._job_attempts(exchange, run_id))
        with_attempts = tuple(
            f.model_copy(update={"run_attempt": attempts[f.check_run_id]})
            if f.check_run_id in attempts
            else f
            for f in facts
        )
        required: tuple[str, ...] | None = None
        if command.base_ref is not None:
            required = await self._required_contexts(exchange, command.base_ref)
        return exchange.completed(
            etag=etag, check_runs=with_attempts, required_contexts=required
        )

    async def _required_contexts(
        self, exchange: _Exchange, base_ref: str
    ) -> tuple[str, ...]:
        """The base branch's required status contexts: classic protection plus rulesets."""
        repository = exchange.command.repository
        branch = await exchange.send(branch_request(repository, base_ref))
        rules = await exchange.send(branch_rules_request(repository, base_ref))
        try:
            contexts = required_contexts_from_branch_body(branch.body)
            contexts |= required_contexts_from_rules_body(rules.body)
        except ValueError as exc:
            raise exchange.failed(
                EnumPrLandingGithubFailureReason.VALIDATION_FAILED,
                f"unreadable required contexts of {base_ref}: {exc}",
                http_status=rules.status,
            ) from None
        return tuple(sorted(contexts))

    def _check_runs(
        self, exchange: _Exchange, response: ModelGithubHttpResponse
    ) -> tuple[ModelGithubCheckRunFact, ...]:
        try:
            return ModelGithubCheckRunFact.from_check_runs_body(response.body)
        except (KeyError, TypeError, ValueError) as exc:
            raise exchange.failed(
                EnumPrLandingGithubFailureReason.VALIDATION_FAILED,
                f"unreadable check-runs response: {exc}",
                http_status=response.status,
            ) from None

    async def _job_attempts(self, exchange: _Exchange, run_id: int) -> dict[int, int]:
        repository = exchange.command.repository
        attempts: dict[int, int] = {}
        for page in range(1, _MAX_PAGES + 1):
            response = await exchange.send(
                run_jobs_request(repository, run_id, page=page)
            )
            try:
                page_attempts = job_attempts_from_jobs_body(response.body)
            except ValueError as exc:
                raise exchange.failed(
                    EnumPrLandingGithubFailureReason.VALIDATION_FAILED,
                    f"unreadable jobs response for run {run_id}: {exc}",
                    http_status=response.status,
                ) from None
            attempts.update(page_attempts)
            if len(page_attempts) < LIST_PAGE_SIZE:
                break
        return attempts

    async def _arm_or_enqueue(
        self, exchange: _Exchange
    ) -> ModelPrLandingGithubCompleted:
        command = exchange.command
        policy_read, mutation = command.to_http_requests()
        response = await exchange.send(policy_read)
        try:
            state = ModelGithubPrStateFact.from_policy_read(response.body)
        except (GithubPrStateParseError, ValueError) as exc:
            raise exchange.failed(
                EnumPrLandingGithubFailureReason.VALIDATION_FAILED,
                f"unreadable landing policy read: {exc}",
                http_status=response.status,
            ) from None
        refusal = _arm_refusal(command, state)
        if refusal is not None:
            reason, detail = refusal
            raise exchange.failed(reason, detail, pr_state=state)
        if (
            command.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
            and state.auto_merge_armed
        ):
            # Already armed at the expected head (another arm path placed it):
            # the arm holds, so the mutation is not sent again (OMN-20866).
            return exchange.completed(pr_state=state)
        if command.operation is EnumPrLandingGithubOperation.ENQUEUE and (
            state.in_merge_queue or state.auto_merge_armed
        ):
            # Already queued, or armed so GitHub queues it when its required
            # checks pass (auto-merge.yml's path): the enqueue holds, so the
            # mutation is not sent again (OMN-20866).
            return exchange.completed(pr_state=state)
        if (
            command.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE
            and state.mergeable_state in _MERGEABLE_NOW
        ):
            # GitHub refuses to arm a PR it would merge now: merge it at the
            # expected head instead of leaving it green and unarmed (OMN-20866).
            mutation = merge_at_head_request(
                state.pr_node_id, command.merge_method, command.required_head_sha()
            )
        await exchange.send(mutation)
        return exchange.completed(pr_state=state)


def _arm_refusal(
    command: ModelPrLandingGithubRequest, state: ModelGithubPrStateFact
) -> tuple[EnumPrLandingGithubFailureReason, str] | None:
    """Why an arm or enqueue must not be sent, judged on its own read, or None."""
    reasons = EnumPrLandingGithubFailureReason
    if state.merged or state.state != "open":
        merged = "merged" if state.merged else "closed"
        return reasons.PR_NOT_OPEN, f"the PR is {merged}"
    expected = command.required_head_sha()
    if state.head_sha != expected:
        return (
            reasons.HEAD_MOVED,
            f"head is {state.head_sha}, the command expected {expected}",
        )
    if state.draft:
        return reasons.DRAFT, "the PR is a draft"
    hold = evaluate_merge_hold(title=state.title, labels=state.labels)
    if not hold.is_merge_eligible:
        return reasons.HELD, hold.reason
    if state.pr_node_id != command.pr_node_id:
        return (
            reasons.VALIDATION_FAILED,
            f"pr_node_id {command.pr_node_id} is not the PR's id {state.pr_node_id}",
        )
    if command.operation is EnumPrLandingGithubOperation.ENQUEUE:
        if not state.merge_queue_enabled:
            return (
                reasons.MERGE_QUEUE_NOT_ENABLED,
                f"base branch {state.base_ref} has no merge queue",
            )
        return None
    if not state.auto_merge_allowed:
        return (
            reasons.AUTO_MERGE_NOT_ALLOWED,
            "the repository does not allow auto-merge",
        )
    if state.merge_queue_enabled:
        return (
            reasons.MERGE_QUEUE_REQUIRED,
            f"base branch {state.base_ref} has a merge queue; the policy is enqueue",
        )
    return None


__all__: list[str] = ["HandlerPrLandingGithubEffect"]

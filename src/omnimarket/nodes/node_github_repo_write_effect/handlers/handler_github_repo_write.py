# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler of node_github_repo_write_effect (OMN-20912).

One typed write command in, one typed completed or failed result out. Every
GitHub call goes through the shared landing transport
(``omnimarket.github_landing``), the send path node_pr_landing_github_effect
uses; no ``gh`` subprocess.

Rules this handler enforces:

- **Mode from the contract.** ``write_config`` names the mode per repository;
  a repository not named takes the default (dry_run). dry_run returns the
  requests the write would send and sends nothing. A request may force
  dry_run; it can never force enforce.
- **Quota floor.** The landing effect's rule, read from this contract's
  ``quota_floor``: every response's ``x-ratelimit-*`` headers become a reading
  per resource, and a call whose resource's last reading is under the floor,
  inside that reading's window, is refused with ``quota_floor`` before it is
  sent.
- **Expected head.** pr_edit, pr_ready and pr_close read the PR first and
  refuse with ``head_moved`` when its head is not the expected one; pr_create
  and workflow_dispatch check the branch tip when given an expected head, and
  pr_comment checks the PR's head when given one. No mutation follows.
- **Idempotency.** A retried command with the same idempotency key returns the
  first result marked ``replayed`` and sends nothing. Across restarts GitHub
  itself is the record: pr_create finds an open PR for the head, pr_comment
  finds a comment carrying the key, pr_ready and pr_close find the PR already
  in the asked-for state. workflow_dispatch has no GitHub-side record, so only
  the in-process record guards a retry.
- **Claim.** pr_close is refused with ``claim_not_held`` unless the requesting
  lane and run hold a live claim on the PR in node_pr_claim_registry_effect.
  The claim is checked in dry_run too, since it needs no GitHub call.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.events.pr_landing_github.model_github_quota_reading import (
    ModelGithubQuotaHeadersMissingError,
    ModelGithubQuotaReading,
)
from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    convert_to_draft_request,
    create_issue_comment_request,
    create_pull_request_request,
    edit_pull_request_request,
    git_ref_request,
    issue_comments_request,
    mark_ready_request,
    next_page_request,
    open_pulls_for_head_request,
    pull_request_request,
    workflow_dispatch_request,
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
from omnimarket.models.model_github_quota_floor import ModelGithubQuotaFloor
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_github_repo_write_effect.handlers.handler_pr_claim_lookup import (
    HandlerPrClaimLookup,
)
from omnimarket.nodes.node_github_repo_write_effect.models import (
    EnumRepoWriteMode,
    EnumRepoWriteOperation,
    EnumRepoWriteRefusal,
    ModelRepoWriteCompleted,
    ModelRepoWriteConfig,
    ModelRepoWriteFailed,
    ModelRepoWriteRequest,
)
from omnimarket.nodes.node_github_repo_write_effect.protocols import (
    ProtocolGithubRepoWriteTransport,
    ProtocolPrClaimLookup,
)

_log = logging.getLogger(__name__)
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_SECRET_NAME = "GITHUB_TOKEN"
# How many completed writes the in-process idempotency record keeps.
_IDEMPOTENCY_RECORD_SIZE = 4096
# Comment pages scanned for an idempotency marker (300 comments).
_MARKER_SCAN_PAGES = 3
# A planned node id in dry_run: the real one is read from the PR first.
_PLANNED_NODE_ID = "<node id read from the pull request>"

Result = ModelRepoWriteCompleted | ModelRepoWriteFailed


def idempotency_marker(key: str) -> str:
    """The hidden line a pr_comment carries so a retry can find it."""
    return f"<!-- onex-idempotency-key: {key} -->"


def _resource_of(request: ModelGithubHttpRequest) -> str:
    return "graphql" if request.path == GITHUB_GRAPHQL_PATH else "core"


def _str_at(body: dict[str, object] | None, *path: str) -> str | None:
    node: object = body
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, str) else None


def _int_at(body: dict[str, object] | None, key: str) -> int | None:
    value = (body or {}).get(key)
    return value if isinstance(value, int) else None


class _RefusedError(Exception):
    """Internal: carries the typed failure out of a multi-request operation."""

    def __init__(self, failed: ModelRepoWriteFailed) -> None:
        super().__init__(failed.detail)
        self.failed = failed


class _Run:
    """One command's exchange: the requests it sent, their statuses, its quota."""

    def __init__(
        self,
        handler: HandlerGithubRepoWriteEffect,
        command: ModelRepoWriteRequest,
        transport: ProtocolGithubRepoWriteTransport,
    ) -> None:
        self._handler = handler
        self.command = command
        self._transport = transport
        self.sent: list[ModelGithubHttpRequest] = []
        self.statuses: list[int] = []
        self.quota: ModelGithubQuotaReading | None = None

    def refused(
        self,
        reason: EnumRepoWriteRefusal,
        detail: str,
        *,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
        quota: ModelGithubQuotaReading | None = None,
    ) -> _RefusedError:
        c = self.command
        return _RefusedError(
            ModelRepoWriteFailed(
                correlation_id=c.correlation_id,
                operation=c.operation,
                repo=c.repo,
                mode=EnumRepoWriteMode.ENFORCE,
                idempotency_key=c.idempotency_key,
                requested_by_lane=c.requested_by_lane,
                pr_number=c.pr_number,
                requests=tuple(self.sent),
                http_statuses=tuple(self.statuses),
                quota=quota if quota is not None else self.quota,
                reason=reason,
                detail=detail or reason.value,
                http_status=http_status,
                retry_after_seconds=retry_after_seconds,
            )
        )

    def completed(
        self,
        *,
        pr_number: int | None = None,
        replayed: bool = False,
        html_url: str | None = None,
        head_sha: str | None = None,
        comment_id: int | None = None,
    ) -> ModelRepoWriteCompleted:
        c = self.command
        return ModelRepoWriteCompleted(
            correlation_id=c.correlation_id,
            operation=c.operation,
            repo=c.repo,
            mode=EnumRepoWriteMode.ENFORCE,
            idempotency_key=c.idempotency_key,
            requested_by_lane=c.requested_by_lane,
            pr_number=pr_number if pr_number is not None else c.pr_number,
            requests=tuple(self.sent),
            http_statuses=tuple(self.statuses),
            quota=self.quota,
            replayed=replayed,
            html_url=html_url,
            head_sha=head_sha,
            comment_id=comment_id,
        )

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        """Send one request; raise :class:`_RefusedError` with the typed failure."""
        resource = _resource_of(request)
        floor_reading = self._handler.floor_refusal(resource)
        if floor_reading is not None:
            raise self.refused(
                EnumRepoWriteRefusal.QUOTA_FLOOR,
                (
                    f"{resource} remaining {floor_reading.remaining} is under the "
                    f"contract floor until {floor_reading.reset}; no call made"
                ),
                quota=floor_reading,
            )
        try:
            response = await self._transport.send(request)
        except (GithubLandingTransportError, OSError) as exc:
            raise self.refused(EnumRepoWriteRefusal.TRANSPORT_ERROR, str(exc)) from None
        self.sent.append(request)
        self.statuses.append(response.status)
        try:
            reading = ModelGithubQuotaReading.from_response_headers(
                response.headers, identity=self._handler.identity
            )
        except ModelGithubQuotaHeadersMissingError:
            pass  # a response without quota headers keeps the last reading
        else:
            self.quota = reading
            self._handler.record_reading(reading)
        errors = (response.body or {}).get("errors")
        if 200 <= response.status < 300 and not errors:
            return response
        reason = (
            EnumRepoWriteRefusal.NOT_FOUND
            if response.status == 404
            else EnumRepoWriteRefusal.GITHUB_REFUSED
        )
        raise self.refused(
            reason,
            f"{request.method} {request.path}: {response.message()}",
            http_status=response.status,
            retry_after_seconds=response.retry_after_seconds(),
        )

    async def expect_pr_head(self) -> dict[str, object]:
        """Read the PR and refuse ``head_moved`` when its head is not the expected one."""
        c = self.command
        number = c.pr_number
        if number is None:  # defensive; the request validator enforces this
            raise ValueError("this operation requires pr_number")
        pr = (await self.send(pull_request_request(c.repo, number))).body or {}
        head = _str_at(pr, "head", "sha")
        if c.expected_head_sha is not None and head != c.expected_head_sha:
            raise self.refused(
                EnumRepoWriteRefusal.HEAD_MOVED,
                f"{c.repo}#{number} head is {head}, expected {c.expected_head_sha}; "
                "no mutation sent",
            )
        return pr

    async def expect_branch_tip(self, branch: str) -> None:
        """Refuse ``head_moved`` when ``branch``'s tip is not the expected head."""
        c = self.command
        if c.expected_head_sha is None:
            return
        ref = (await self.send(git_ref_request(c.repo, branch))).body or {}
        tip = _str_at(ref, "object", "sha")
        if tip != c.expected_head_sha:
            raise self.refused(
                EnumRepoWriteRefusal.HEAD_MOVED,
                f"{c.repo} {branch} tip is {tip}, expected {c.expected_head_sha}; "
                "no mutation sent",
            )


class HandlerGithubRepoWriteEffect:
    """EFFECT: run one PR authoring write against GitHub and report it typed.

    ``transport`` and ``claim_lookup`` are injected in tests (a recorded fake
    and a stub). Without a transport the handler builds the live urllib
    transport per call from the contract-declared ``GITHUB_TOKEN`` ref.
    """

    def __init__(
        self,
        transport: ProtocolGithubRepoWriteTransport | None = None,
        *,
        claim_lookup: ProtocolPrClaimLookup | None = None,
        quota_floor: ModelGithubQuotaFloor | None = None,
        config: ModelRepoWriteConfig | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._claims = claim_lookup or HandlerPrClaimLookup()
        self._floor = quota_floor or ModelGithubQuotaFloor.from_contract(_CONTRACT_PATH)
        self._config = config or ModelRepoWriteConfig.from_contract(_CONTRACT_PATH)
        self._clock = clock
        self.identity = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
        self._readings: dict[str, ModelGithubQuotaReading] = {}
        self._done: OrderedDict[tuple[str, str, str], ModelRepoWriteCompleted] = (
            OrderedDict()
        )

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

    async def handle(self, request: ModelRepoWriteRequest) -> Result:
        """Run one write; dry_run plans its requests and sends nothing."""
        key = (request.repo.lower(), request.operation.value, request.idempotency_key)
        done = self._done.get(key)
        if done is not None:
            return done.model_copy(
                update={"correlation_id": request.correlation_id, "replayed": True}
            )
        refused = self._claim_refusal(request)
        if refused is not None:
            return refused
        mode = (
            EnumRepoWriteMode.DRY_RUN
            if request.dry_run
            else self._config.mode_for(request.repo)
        )
        if mode is EnumRepoWriteMode.DRY_RUN:
            return ModelRepoWriteCompleted(
                correlation_id=request.correlation_id,
                operation=request.operation,
                repo=request.repo,
                mode=mode,
                idempotency_key=request.idempotency_key,
                requested_by_lane=request.requested_by_lane,
                pr_number=request.pr_number,
                requests=planned_requests(request),
            )
        transport = self._transport or await self._live_transport()
        run = _Run(self, request, transport)
        try:
            result: Result = await self._run(run)
        except _RefusedError as exc:
            result = exc.failed
        if isinstance(result, ModelRepoWriteCompleted):
            self._done[key] = result
            while len(self._done) > _IDEMPOTENCY_RECORD_SIZE:
                self._done.popitem(last=False)
        _log.info(
            "repo-write %s %s lane=%s -> %s",
            request.operation.value,
            request.repo,
            request.requested_by_lane,
            getattr(result, "reason", result.outcome),
        )
        return result

    def _claim_refusal(
        self, request: ModelRepoWriteRequest
    ) -> ModelRepoWriteFailed | None:
        if request.operation is not EnumRepoWriteOperation.PR_CLOSE:
            return None
        claims_dir = request.claims_dir or ""
        now = datetime.fromtimestamp(self._clock(), tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        claim = self._claims.active_claim(
            claims_dir=claims_dir, pr_key=request.pr_key, now=now
        )
        if claim is None:
            detail = f"no live claim on {request.pr_key}; pr_close needs one"
        elif claim.claimed_by_run != request.requested_by_run or (
            claim.lane_id and claim.lane_id != request.requested_by_lane
        ):
            detail = (
                f"{request.pr_key} is claimed by run {claim.claimed_by_run} lane "
                f"{claim.lane_id}, not run {request.requested_by_run} lane "
                f"{request.requested_by_lane}"
            )
        else:
            return None
        return ModelRepoWriteFailed(
            correlation_id=request.correlation_id,
            operation=request.operation,
            repo=request.repo,
            mode=EnumRepoWriteMode.DRY_RUN
            if request.dry_run
            else self._config.mode_for(request.repo),
            idempotency_key=request.idempotency_key,
            requested_by_lane=request.requested_by_lane,
            pr_number=request.pr_number,
            reason=EnumRepoWriteRefusal.CLAIM_NOT_HELD,
            detail=detail,
        )

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

    async def _run(self, run: _Run) -> ModelRepoWriteCompleted:
        op = run.command.operation
        if op is EnumRepoWriteOperation.PR_CREATE:
            return await _pr_create(run)
        if op is EnumRepoWriteOperation.PR_EDIT:
            return await _pr_edit(run)
        if op is EnumRepoWriteOperation.PR_READY:
            return await _pr_ready(run)
        if op is EnumRepoWriteOperation.PR_CLOSE:
            return await _pr_close(run)
        if op is EnumRepoWriteOperation.PR_COMMENT:
            return await _pr_comment(run)
        return await _workflow_dispatch(run)


# --- operations ----------------------------------------------------------------


async def _pr_create(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    head, base, title = c.head or "", c.base or "", c.title or ""
    await run.expect_branch_tip(head)
    existing = (await run.send(open_pulls_for_head_request(c.repo, head))).body or {}
    prs = existing.get("value")
    if isinstance(prs, list) and prs and isinstance(prs[0], dict):
        pr = prs[0]
        return run.completed(
            pr_number=_int_at(pr, "number"),
            replayed=True,
            html_url=_str_at(pr, "html_url"),
            head_sha=_str_at(pr, "head", "sha"),
        )
    created = (
        await run.send(
            create_pull_request_request(
                c.repo,
                title=title,
                head=head,
                base=base,
                body=c.body or "",
                draft=bool(c.draft),
            )
        )
    ).body
    return run.completed(
        pr_number=_int_at(created, "number"),
        html_url=_str_at(created, "html_url"),
        head_sha=_str_at(created, "head", "sha"),
    )


async def _pr_edit(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    number = c.pr_number or 0
    pr = await run.expect_pr_head()
    fields: dict[str, object] = {}
    if c.title is not None and c.title != pr.get("title"):
        fields["title"] = c.title
    if c.body is not None and c.body != (pr.get("body") or ""):
        fields["body"] = c.body
    if c.base is not None and c.base != _str_at(pr, "base", "ref"):
        fields["base"] = c.base
    changed = bool(fields)
    if fields:
        await run.send(edit_pull_request_request(c.repo, number, fields))
    node_id = _str_at(pr, "node_id") or ""
    is_draft = bool(pr.get("draft", False))
    if c.draft is True and not is_draft:
        await run.send(convert_to_draft_request(node_id))
        changed = True
    elif c.draft is False and is_draft:
        await run.send(mark_ready_request(node_id))
        changed = True
    return run.completed(
        replayed=not changed,
        html_url=_str_at(pr, "html_url"),
        head_sha=_str_at(pr, "head", "sha"),
    )


async def _pr_ready(run: _Run) -> ModelRepoWriteCompleted:
    pr = await run.expect_pr_head()
    already = not bool(pr.get("draft", False))
    if not already:
        await run.send(mark_ready_request(_str_at(pr, "node_id") or ""))
    return run.completed(
        replayed=already,
        html_url=_str_at(pr, "html_url"),
        head_sha=_str_at(pr, "head", "sha"),
    )


async def _pr_close(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    pr = await run.expect_pr_head()
    already = pr.get("state") == "closed"
    if not already:
        await run.send(
            edit_pull_request_request(c.repo, c.pr_number or 0, {"state": "closed"})
        )
    return run.completed(
        replayed=already,
        html_url=_str_at(pr, "html_url"),
        head_sha=_str_at(pr, "head", "sha"),
    )


async def _pr_comment(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    number = c.pr_number or 0
    head_sha: str | None = None
    if c.expected_head_sha is not None:
        head_sha = _str_at(await run.expect_pr_head(), "head", "sha")
    marker = idempotency_marker(c.idempotency_key)
    request: ModelGithubHttpRequest | None = issue_comments_request(
        c.repo, number, per_page=LIST_PAGE_SIZE
    )
    for _page in range(_MARKER_SCAN_PAGES):
        if request is None:
            break
        response = await run.send(request)
        page = (response.body or {}).get("value")
        for comment in page if isinstance(page, list) else []:
            if isinstance(comment, dict) and marker in str(comment.get("body", "")):
                return run.completed(
                    replayed=True,
                    comment_id=_int_at(comment, "id"),
                    html_url=_str_at(comment, "html_url"),
                    head_sha=head_sha,
                )
        request = next_page_request(response)
    created = (
        await run.send(
            create_issue_comment_request(c.repo, number, f"{c.body}\n\n{marker}")
        )
    ).body
    return run.completed(
        comment_id=_int_at(created, "id"),
        html_url=_str_at(created, "html_url"),
        head_sha=head_sha,
    )


async def _workflow_dispatch(run: _Run) -> ModelRepoWriteCompleted:
    c = run.command
    ref = c.ref or ""
    await run.expect_branch_tip(ref)
    await run.send(workflow_dispatch_request(c.repo, c.workflow or "", ref, c.inputs))
    return run.completed(head_sha=c.expected_head_sha)


def planned_requests(
    request: ModelRepoWriteRequest,
) -> tuple[ModelGithubHttpRequest, ...]:
    """The requests an enforce run would send, before any answer is known.

    A request that depends on an answer (the PR's node id for a GraphQL
    mutation) carries a placeholder; a read that would find the write already
    done is listed, and the write after it is listed too.
    """
    c = request
    n = c.pr_number or 0
    op = c.operation
    tip = (
        [
            git_ref_request(
                c.repo,
                (c.head if op is EnumRepoWriteOperation.PR_CREATE else c.ref) or "",
            )
        ]
        if c.expected_head_sha is not None
        and op
        in (EnumRepoWriteOperation.PR_CREATE, EnumRepoWriteOperation.WORKFLOW_DISPATCH)
        else []
    )
    if op is EnumRepoWriteOperation.PR_CREATE:
        return (
            *tip,
            open_pulls_for_head_request(c.repo, c.head or ""),
            create_pull_request_request(
                c.repo,
                title=c.title or "",
                head=c.head or "",
                base=c.base or "",
                body=c.body or "",
                draft=bool(c.draft),
            ),
        )
    if op is EnumRepoWriteOperation.WORKFLOW_DISPATCH:
        return (
            *tip,
            workflow_dispatch_request(c.repo, c.workflow or "", c.ref or "", c.inputs),
        )
    if op is EnumRepoWriteOperation.PR_COMMENT:
        read = [pull_request_request(c.repo, n)] if c.expected_head_sha else []
        return (
            *read,
            issue_comments_request(c.repo, n, per_page=LIST_PAGE_SIZE),
            create_issue_comment_request(
                c.repo, n, f"{c.body}\n\n{idempotency_marker(c.idempotency_key)}"
            ),
        )
    read_pr = pull_request_request(c.repo, n)
    if op is EnumRepoWriteOperation.PR_READY:
        return (read_pr, mark_ready_request(_PLANNED_NODE_ID))
    if op is EnumRepoWriteOperation.PR_CLOSE:
        return (read_pr, edit_pull_request_request(c.repo, n, {"state": "closed"}))
    fields = {
        k: v
        for k, v in (("title", c.title), ("body", c.body), ("base", c.base))
        if v is not None
    }
    planned: list[ModelGithubHttpRequest] = [read_pr]
    if fields:
        planned.append(edit_pull_request_request(c.repo, n, dict(fields)))
    if c.draft is True:
        planned.append(convert_to_draft_request(_PLANNED_NODE_ID))
    elif c.draft is False:
        planned.append(mark_ready_request(_PLANNED_NODE_ID))
    return tuple(planned)


__all__: list[str] = [
    "HandlerGithubRepoWriteEffect",
    "idempotency_marker",
    "planned_requests",
]

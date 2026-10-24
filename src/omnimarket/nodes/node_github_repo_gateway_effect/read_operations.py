# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-operation read functions for node_github_repo_gateway_effect.

Each function reads exactly one operation from an injected transport and returns
its own typed result. No read function calls another — the parent dispatcher is
the only caller. A shared private ``_classify_required_checks`` helper is used by
the two check-oriented reads; it is not itself an operation.

The OMN-20912 reads page through ``_collect``: it follows ``Link: rel="next"``
until the request's limit is met, so a caller names how many items it wants and
never a page. Every list result says whether it was cut (``truncated``); a log
keeps only its tail and an artifact archive is fetched only under the cap.
"""

from __future__ import annotations

import base64
import hashlib
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel

from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    artifact_archive_request,
    artifact_request,
    commit_check_runs_request,
    issue_comments_request,
    job_log_request,
    job_request,
    next_page_request,
    pull_request_request,
    pull_requests_request,
    releases_request,
    review_threads_request,
    run_artifacts_request,
    run_jobs_request,
    tags_request,
    workflow_runs_request,
)
from omnimarket.github_landing.github_landing_transport import JobLogWindow, step_second
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubBytesResponse,
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.models.model_github_gateway_io import (
    DEFAULT_LOG_TAIL_BYTES,
    MAX_TEXT_CHARS,
    EnumArtifactFetchRefusal,
    ModelArtifactContent,
    ModelArtifactsResult,
    ModelArtifactSummary,
    ModelBranchProtectionResult,
    ModelCheckRun,
    ModelCiChecksResult,
    ModelCommitCheckRunsResult,
    ModelGithubGatewayRequest,
    ModelJobLogTailResult,
    ModelJobStep,
    ModelMergeCommitShaResult,
    ModelMergedPrResult,
    ModelOpenPrsResult,
    ModelOpenPrSummary,
    ModelPrComment,
    ModelPrHead,
    ModelPrHeadsResult,
    ModelPrReviewThread,
    ModelPrReviewThreadComment,
    ModelPrReviewThreadsResult,
    ModelPrStatusResult,
    ModelPrTextResult,
    ModelRelease,
    ModelReleasesResult,
    ModelReviewGateResult,
    ModelRunJob,
    ModelRunJobsResult,
    ModelTag,
    ModelTicketRefResult,
    ModelWorkflowRunsResult,
    ModelWorkflowRunSummary,
)
from omnimarket.nodes.node_github_repo_gateway_effect.transport import (
    GitHubReadTransportProtocol,
)
from omnimarket.nodes.node_merge_sweep_compute.protocols import GitHubTransportError

# A runaway paging guard: MAX_LIST_LIMIT items need at most 3 pages of 100.
_MAX_PAGES = 10

_OverallState = Literal["green", "red", "pending"]

# Conclusions/statuses that mark a required check as passed or failed. Mirrors
# node_merge_sweep_compute's required-check classification so the gateway agrees
# with the merge sweep on what "green" means.
_PASS_CONCLUSIONS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_FAILED_CONCLUSIONS = frozenset(
    {
        "ACTION_REQUIRED",
        "CANCELLED",
        "FAILURE",
        "FAILED",
        "STARTUP_FAILURE",
        "STALE",
        "TIMED_OUT",
    }
)
_FAILED_STATUSES = frozenset({"FAILURE", "ERROR"})
_TICKET_PATTERN = re.compile(r"(OMN|omn)-\d+", re.IGNORECASE)


def _classify_required_checks(
    rollup: list[dict[str, Any]],
) -> tuple[_OverallState, list[str], int, int, int, int]:
    """Return (overall, failing_names, total, passed, failed, pending).

    ``overall`` is green when no required check failed or is pending, red when
    any failed, pending otherwise. With no required checks the state is green.
    """
    required = [c for c in rollup if isinstance(c, dict) and c.get("isRequired")]
    total = len(required)
    if total == 0:
        return "green", [], 0, 0, 0, 0

    failing_names: list[str] = []
    passed = failed = pending = 0
    for check in required:
        conclusion = str(check.get("conclusion") or "").upper()
        status = str(check.get("status") or "").upper()
        name = str(check.get("name") or "")
        if conclusion in _PASS_CONCLUSIONS or status == "SUCCESS":
            passed += 1
        elif conclusion in _FAILED_CONCLUSIONS or status in _FAILED_STATUSES:
            failed += 1
            failing_names.append(name)
        else:
            pending += 1

    if failed:
        overall: _OverallState = "red"
    elif pending:
        overall = "pending"
    else:
        overall = "green"
    return overall, failing_names, total, passed, failed, pending


def _normalize_merge_state(value: object) -> str:
    normalized = str(value or "UNKNOWN").upper()
    return normalized or "UNKNOWN"


def read_pr_status(
    transport: GitHubReadTransportProtocol, repo: str, pr_number: int
) -> ModelPrStatusResult:
    """Merge-readiness summary for one PR."""
    pr = transport.fetch_pr_detail(repo, pr_number)
    overall, failing, _total, _passed, _failed, _pending = _classify_required_checks(
        pr.get("statusCheckRollup") or []
    )
    merge_state = _normalize_merge_state(pr.get("mergeStateStatus"))
    blocked = overall != "green" or merge_state != "CLEAN"
    return ModelPrStatusResult(
        repo=repo,
        pr_number=pr_number,
        overall=overall,
        blocked=blocked,
        merge_state_status=merge_state,
        review_decision=pr.get("reviewDecision"),
        failing_contexts=failing,
    )


def read_ci_checks(
    transport: GitHubReadTransportProtocol, repo: str, pr_number: int
) -> ModelCiChecksResult:
    """Required-check rollup with per-state counts for one PR."""
    pr = transport.fetch_pr_detail(repo, pr_number)
    overall, failing, total, passed, failed, pending = _classify_required_checks(
        pr.get("statusCheckRollup") or []
    )
    return ModelCiChecksResult(
        repo=repo,
        pr_number=pr_number,
        overall=overall,
        total=total,
        passed=passed,
        failed=failed,
        pending=pending,
        failing_contexts=failing,
    )


def read_open_prs_list(
    transport: GitHubReadTransportProtocol, repo: str
) -> ModelOpenPrsResult:
    """List open PRs for a repo (repo-scoped canary read)."""
    raw = transport.fetch_open_prs(repo)
    prs = [
        ModelOpenPrSummary(
            number=pr["number"],
            title=str(pr.get("title", "")),
            is_draft=bool(pr.get("isDraft", False)),
            merge_state_status=_normalize_merge_state(pr.get("mergeStateStatus")),
            review_decision=pr.get("reviewDecision"),
        )
        for pr in raw
    ]
    return ModelOpenPrsResult(repo=repo, count=len(prs), prs=prs)


def read_branch_protection(
    transport: GitHubReadTransportProtocol, repo: str
) -> ModelBranchProtectionResult:
    """Required approving review count for a repo (repo-scoped canary read)."""
    count = transport.fetch_branch_protection(repo)
    return ModelBranchProtectionResult(repo=repo, required_approving_review_count=count)


def read_review_gate(
    transport: GitHubReadTransportProtocol, repo: str, pr_number: int
) -> ModelReviewGateResult:
    """Review-gate state for one PR."""
    pr = transport.fetch_pr_detail(repo, pr_number)
    threads = pr.get("reviewThreads") or []
    unresolved = sum(1 for t in threads if not t.get("isResolved", False))
    review_decision = pr.get("reviewDecision")
    blocked = review_decision == "CHANGES_REQUESTED" or unresolved > 0
    return ModelReviewGateResult(
        repo=repo,
        pr_number=pr_number,
        review_decision=review_decision,
        unresolved_threads=unresolved,
        blocked=blocked,
    )


def read_merge_commit_sha(
    transport: GitHubReadTransportProtocol, repo: str, pr_number: int
) -> ModelMergeCommitShaResult:
    """Merge outcome (merged flag + merge commit SHA) for one PR."""
    pr = transport.fetch_pr_detail(repo, pr_number)
    return ModelMergeCommitShaResult(
        repo=repo,
        pr_number=pr_number,
        merged=bool(pr.get("merged", False)),
        merge_commit_sha=pr.get("mergeCommitOid"),
    )


def read_ticket_ref(
    transport: GitHubReadTransportProtocol, repo: str, pr_number: int
) -> ModelTicketRefResult:
    """Linear ticket reference extracted from a PR head branch."""
    pr = transport.fetch_pr_detail(repo, pr_number)
    head_ref = str(pr.get("headRefName", ""))
    match = _TICKET_PATTERN.search(head_ref)
    return ModelTicketRefResult(
        repo=repo,
        pr_number=pr_number,
        head_ref=head_ref,
        ticket_id=match.group(0).upper() if match else None,
    )


# --- OMN-20912: Actions, PR text and release reads ---------------------------


def _checked(
    request: ModelGithubHttpRequest, response: ModelGithubHttpResponse
) -> dict[str, object]:
    """The response body of a 2xx answer; any other status raises."""
    if not 200 <= response.status < 300:
        raise GitHubTransportError(
            f"GitHub {request.method} {request.path} answered HTTP "
            f"{response.status}: {response.message()}"
        )
    return response.body or {}


def _collect(
    transport: GitHubReadTransportProtocol,
    first: ModelGithubHttpRequest,
    *,
    item_key: str,
    limit: int,
) -> tuple[list[dict[str, object]], int | None, bool]:
    """Page a list read up to ``limit`` items: (items, GitHub total, truncated).

    ``item_key`` names the array in an object body (``workflow_runs``); an
    array body arrives from the transport wrapped as ``value``.
    """
    items: list[dict[str, object]] = []
    total: int | None = None
    request: ModelGithubHttpRequest | None = first
    for _page in range(_MAX_PAGES):
        if request is None:
            return items, total, False
        response = transport.send_sync(request)
        body = _checked(request, response)
        raw_total = body.get("total_count")
        if total is None and isinstance(raw_total, int):
            total = raw_total
        page = body.get(item_key)
        if isinstance(page, list):
            items.extend(item for item in page if isinstance(item, dict))
        request = next_page_request(response)
        if len(items) > limit:
            return items[:limit], total, True
        if len(items) == limit:
            return items, total, request is not None
    return items[:limit], total, request is not None


def _pick[ModelT: BaseModel](model: type[ModelT], raw: dict[str, object]) -> ModelT:
    """Validate ``model`` from the keys of ``raw`` it declares; others are dropped."""
    return model.model_validate({k: raw[k] for k in model.model_fields if k in raw})


def _login(raw: object) -> str:
    if isinstance(raw, dict):
        login = raw.get("login")
        if isinstance(login, str):
            return login
    return ""


def _nested_str(raw: object, key: str) -> str:
    if isinstance(raw, dict):
        value = raw.get(key)
        if isinstance(value, str):
            return value
    return ""


def _cut(text: object) -> tuple[str, bool]:
    value = text if isinstance(text, str) else ""
    return value[:MAX_TEXT_CHARS], len(value) > MAX_TEXT_CHARS


def read_workflow_runs(
    transport: GitHubReadTransportProtocol, request: ModelGithubGatewayRequest
) -> ModelWorkflowRunsResult:
    """Workflow runs of a repo or one workflow, newest first (``gh run list``)."""
    first = workflow_runs_request(
        request.repo,
        per_page=min(request.limit, LIST_PAGE_SIZE),
        workflow=request.workflow,
        branch=request.branch,
        head_sha=request.head_sha,
        event=request.event,
        status=request.status,
    )
    raw, total, truncated = _collect(
        transport, first, item_key="workflow_runs", limit=request.limit
    )
    return ModelWorkflowRunsResult(
        repo=request.repo,
        total_count=total,
        truncated=truncated,
        runs=[_pick(ModelWorkflowRunSummary, run) for run in raw],
    )


def read_run_jobs(
    transport: GitHubReadTransportProtocol, request: ModelGithubGatewayRequest
) -> ModelRunJobsResult:
    """Every job of one run across its attempts, with steps (``gh run view``)."""
    run_id = _require(request.run_id, "run_id")
    raw, total, truncated = _collect(
        transport,
        run_jobs_request(request.repo, run_id),
        item_key="jobs",
        limit=request.limit,
    )
    jobs: list[ModelRunJob] = []
    for job in raw:
        steps_raw = job.get("steps")
        steps = [
            _pick(ModelJobStep, step)
            for step in (steps_raw if isinstance(steps_raw, list) else [])
            if isinstance(step, dict)
        ]
        jobs.append(_pick(ModelRunJob, {**job, "steps": steps}))
    return ModelRunJobsResult(
        repo=request.repo,
        run_id=run_id,
        total_count=total,
        truncated=truncated,
        jobs=jobs,
    )


def read_job_log_tail(
    transport: GitHubReadTransportProtocol, request: ModelGithubGatewayRequest
) -> ModelJobLogTailResult:
    """The last ``tail_bytes`` of one job's log, or a window of it (OMN-17427).

    With ``offset_bytes`` or ``step`` set the read keeps ``length_bytes`` from
    that offset of the whole log, or of the named step's region.
    """
    job_id = _require(request.job_id, "job_id")
    if request.offset_bytes is not None or request.step is not None:
        return _read_job_log_window(transport, request, job_id)
    log_request = job_log_request(request.repo, job_id)
    response = transport.send_bytes_sync(
        log_request, limit=request.tail_bytes, keep="tail"
    )
    _check_log(log_request, response)
    return ModelJobLogTailResult(
        repo=request.repo,
        job_id=job_id,
        total_bytes=response.total_bytes,
        truncated=response.over_limit,
        text=response.content.decode("utf-8", errors="replace"),
        start_offset=response.total_bytes - len(response.content),
    )


# An unknown step's refusal lists at most this many step names, each cut.
_MAX_LISTED_STEPS = 60
_MAX_LISTED_STEP_CHARS = 120


def _check_log(
    log_request: ModelGithubHttpRequest, response: ModelGithubBytesResponse
) -> None:
    if not 200 <= response.status < 300:
        raise GitHubTransportError(
            f"GitHub GET {log_request.path} answered HTTP {response.status}: "
            f"{response.content.decode('utf-8', errors='replace')}"
        )


def _job_step(
    transport: GitHubReadTransportProtocol, repo: str, job_id: int, name: str
) -> ModelJobStep:
    """The job's step named exactly ``name``; an unknown name raises."""
    meta_request = job_request(repo, job_id)
    body = _checked(meta_request, transport.send_sync(meta_request))
    raw = body.get("steps")
    steps = [
        _pick(ModelJobStep, step)
        for step in (raw if isinstance(raw, list) else [])
        if isinstance(step, dict)
    ]
    for step in steps:
        if step.name == name:
            return step
    listed = ", ".join(
        repr(step.name[:_MAX_LISTED_STEP_CHARS]) for step in steps[:_MAX_LISTED_STEPS]
    )
    raise GitHubTransportError(
        f"job {job_id} has no step named {name!r}; its steps: {listed or 'none'}"
    )


def _read_job_log_window(
    transport: GitHubReadTransportProtocol,
    request: ModelGithubGatewayRequest,
    job_id: int,
) -> ModelJobLogTailResult:
    start = request.offset_bytes or 0
    length = request.length_bytes or DEFAULT_LOG_TAIL_BYTES
    step: ModelJobStep | None = None
    bounds = None
    if request.step is not None:
        step = _job_step(transport, request.repo, job_id, request.step)
        if step.started_at is None:
            # Skipped or never run: the step wrote no lines.
            return ModelJobLogTailResult(
                repo=request.repo,
                job_id=job_id,
                total_bytes=0,
                truncated=False,
                text="",
                **_step_fields(step),
            )
        started = step_second(step.started_at)
        # A running step has no completion yet: its region runs to the log's end.
        completed = (
            step_second(step.completed_at)
            if step.completed_at is not None
            else datetime.max.replace(tzinfo=UTC)
        )
        bounds = (started, completed)
    window = JobLogWindow(start=start, length=length, step_bounds=bounds)
    log_request = job_log_request(request.repo, job_id)
    response = transport.send_bytes_window_sync(log_request, window=window)
    _check_log(log_request, response)
    return ModelJobLogTailResult(
        repo=request.repo,
        job_id=job_id,
        total_bytes=response.total_bytes,
        truncated=window.truncated,
        text=response.content.decode("utf-8", errors="replace"),
        start_offset=window.start_offset,
        next_offset=window.next_offset,
        read_ceiling_hit=response.over_limit,
        **(
            {
                **_step_fields(step),
                "region_start": window.region_start,
                "region_end": window.region_end,
                "region_bounds": "timestamp",
            }
            if step is not None
            else {}
        ),
    )


def _step_fields(step: ModelJobStep) -> dict[str, Any]:
    return {
        "step_name": step.name,
        "step_number": step.number,
        "step_status": step.status,
        "step_conclusion": step.conclusion,
        "step_started_at": step.started_at,
        "step_completed_at": step.completed_at,
    }


def read_artifacts(
    transport: GitHubReadTransportProtocol, request: ModelGithubGatewayRequest
) -> ModelArtifactsResult:
    """A run's artifacts, and one archive under the cap when asked (``gh run download``).

    The fetch reads the artifact first: one that belongs to another run, has
    expired, or reports a size over ``max_bytes`` is refused with a typed
    reason and never downloaded. A download that runs past the cap anyway is
    refused the same way, with nothing kept.
    """
    run_id = _require(request.run_id, "run_id")
    raw, total, truncated = _collect(
        transport,
        run_artifacts_request(
            request.repo, run_id, per_page=min(request.limit, LIST_PAGE_SIZE)
        ),
        item_key="artifacts",
        limit=request.limit,
    )
    artifacts = [_pick(ModelArtifactSummary, item) for item in raw]
    fetched: ModelArtifactContent | None = None
    refused: EnumArtifactFetchRefusal | None = None
    if request.artifact_id is not None:
        fetched, refused = _fetch_artifact(transport, request, run_id)
    return ModelArtifactsResult(
        repo=request.repo,
        run_id=run_id,
        total_count=total,
        truncated=truncated,
        artifacts=artifacts,
        fetched=fetched,
        fetch_refused=refused,
    )


def _fetch_artifact(
    transport: GitHubReadTransportProtocol,
    request: ModelGithubGatewayRequest,
    run_id: int,
) -> tuple[ModelArtifactContent | None, EnumArtifactFetchRefusal | None]:
    artifact_id = _require(request.artifact_id, "artifact_id")
    meta_request = artifact_request(request.repo, artifact_id)
    meta_response = transport.send_sync(meta_request)
    if meta_response.status == 404:
        return None, EnumArtifactFetchRefusal.NOT_IN_RUN
    meta = _checked(meta_request, meta_response)
    run = meta.get("workflow_run")
    if not isinstance(run, dict) or run.get("id") != run_id:
        return None, EnumArtifactFetchRefusal.NOT_IN_RUN
    if meta.get("expired") is True:
        return None, EnumArtifactFetchRefusal.EXPIRED
    size = meta.get("size_in_bytes")
    if not isinstance(size, int) or size > request.max_bytes:
        return None, EnumArtifactFetchRefusal.OVER_SIZE_CAP
    archive_request = artifact_archive_request(request.repo, artifact_id)
    archive = transport.send_bytes_sync(
        archive_request, limit=request.max_bytes, keep="head"
    )
    if not 200 <= archive.status < 300:
        raise GitHubTransportError(
            f"GitHub GET {archive_request.path} answered HTTP {archive.status}"
        )
    if archive.over_limit:
        return None, EnumArtifactFetchRefusal.OVER_SIZE_CAP
    return (
        ModelArtifactContent(
            artifact_id=artifact_id,
            name=str(meta.get("name", "")),
            size_bytes=len(archive.content),
            sha256=hashlib.sha256(archive.content).hexdigest(),
            content_base64=base64.b64encode(archive.content).decode("ascii"),
        ),
        None,
    )


def read_pr_text(
    transport: GitHubReadTransportProtocol, request: ModelGithubGatewayRequest
) -> ModelPrTextResult:
    """A PR's title, body and conversation comments (``gh pr view --comments``)."""
    pr_number = _require(request.pr_number, "pr_number")
    pr_request = pull_request_request(request.repo, pr_number)
    pr = _checked(pr_request, transport.send_sync(pr_request))
    raw_comments, _total, truncated = _collect(
        transport,
        issue_comments_request(
            request.repo, pr_number, per_page=min(request.limit, LIST_PAGE_SIZE)
        ),
        item_key="value",
        limit=request.limit,
    )
    comments: list[ModelPrComment] = []
    for comment in raw_comments:
        text, cut = _cut(comment.get("body"))
        comments.append(
            ModelPrComment.model_validate(
                {
                    "id": comment.get("id"),
                    "author": _login(comment.get("user")),
                    "created_at": comment.get("created_at"),
                    "updated_at": comment.get("updated_at"),
                    "body": text,
                    "body_truncated": cut,
                }
            )
        )
    body, body_cut = _cut(pr.get("body"))
    raw_count = pr.get("comments")
    head = pr.get("head")
    head_repo = (
        _nested_str(head.get("repo") if isinstance(head, dict) else None, "full_name")
        or None
    )
    return ModelPrTextResult(
        repo=request.repo,
        pr_number=pr_number,
        title=str(pr.get("title", "")),
        body=body,
        body_truncated=body_cut,
        state=str(pr.get("state", "")),
        is_draft=bool(pr.get("draft", False)),
        merged=bool(pr.get("merged", False)),
        author=_login(pr.get("user")),
        head_ref=_nested_str(pr.get("head"), "ref"),
        head_sha=_nested_str(pr.get("head"), "sha"),
        base_ref=_nested_str(pr.get("base"), "ref"),
        head_repo=head_repo,
        is_cross_repository=head_repo is None
        or head_repo.lower() != request.repo.lower(),
        total_count=raw_count if isinstance(raw_count, int) else None,
        truncated=truncated,
        comments=comments,
    )


def read_releases(
    transport: GitHubReadTransportProtocol, request: ModelGithubGatewayRequest
) -> ModelReleasesResult:
    """Releases and tags of a repo, newest first (``gh release list``)."""
    per_page = min(request.limit, LIST_PAGE_SIZE)
    raw_releases, _r_total, releases_cut = _collect(
        transport,
        releases_request(request.repo, per_page=per_page),
        item_key="value",
        limit=request.limit,
    )
    raw_tags, _t_total, tags_cut = _collect(
        transport,
        tags_request(request.repo, per_page=per_page),
        item_key="value",
        limit=request.limit,
    )
    return ModelReleasesResult(
        repo=request.repo,
        releases=[_pick(ModelRelease, item) for item in raw_releases],
        releases_truncated=releases_cut,
        tags=[
            ModelTag(
                name=str(tag.get("name", "")),
                sha=_nested_str(tag.get("commit"), "sha"),
            )
            for tag in raw_tags
        ],
        tags_truncated=tags_cut,
    )


def _pr_head(item: dict[str, object]) -> ModelPrHead | None:
    """One REST pull-request item as a ModelPrHead; None when it has no head commit."""
    head = item.get("head")
    head_oid = _nested_str(head, "sha")
    number = item.get("number")
    if not isinstance(number, int) or len(head_oid) != 40:
        return None
    merged_at = item.get("merged_at")
    merged = isinstance(merged_at, str) and bool(merged_at)
    merge_sha = item.get("merge_commit_sha")
    state: Literal["MERGED", "OPEN", "CLOSED"]
    if merged:
        state = "MERGED"
    elif item.get("state") == "open":
        state = "OPEN"
    else:
        state = "CLOSED"
    return ModelPrHead(
        number=number,
        state=state,
        head_oid=head_oid,
        head_ref=_nested_str(head, "ref") or None,
        base_ref=_nested_str(item.get("base"), "ref") or None,
        # An unmerged PR's merge_commit_sha is GitHub's test merge, not a merge.
        merge_commit_sha=merge_sha if merged and isinstance(merge_sha, str) else None,
        merged_at=merged_at if merged and isinstance(merged_at, str) else None,
    )


def read_pr_heads(
    transport: GitHubReadTransportProtocol, request: ModelGithubGatewayRequest
) -> ModelPrHeadsResult:
    """Every PR's head commit and merge outcome, newest first (OMN-20977).

    Reads pull requests in every state up to the request's limit, then keeps
    those matching ``branch`` and ``head_sha`` when either is given.
    """
    raw, total, cut = _collect(
        transport,
        pull_requests_request(
            request.repo, state="all", per_page=min(request.limit, LIST_PAGE_SIZE)
        ),
        item_key="value",
        limit=request.limit,
    )
    prs = [pr for pr in (_pr_head(item) for item in raw) if pr is not None]
    if request.branch is not None:
        prs = [pr for pr in prs if pr.head_ref == request.branch]
    if request.head_sha is not None:
        prs = [pr for pr in prs if pr.head_oid == request.head_sha]
    return ModelPrHeadsResult(
        repo=request.repo, prs=prs, total_count=total, truncated=cut
    )


def read_commit_check_runs(
    transport: GitHubReadTransportProtocol, request: ModelGithubGatewayRequest
) -> ModelCommitCheckRunsResult:
    """Latest check runs on a commit, optionally filtered by name (OMN-21146)."""
    head_sha = request.head_sha
    if head_sha is None:  # defensive; the request validator already enforces this
        raise ValueError("this operation requires head_sha.")
    raw, total, truncated = _collect(
        transport,
        commit_check_runs_request(
            request.repo,
            head_sha,
            per_page=min(request.limit, LIST_PAGE_SIZE),
            check_name=request.check_name,
        ),
        item_key="check_runs",
        limit=request.limit,
    )
    return ModelCommitCheckRunsResult(
        repo=request.repo,
        head_sha=head_sha,
        check_name=request.check_name,
        total_count=total,
        truncated=truncated,
        check_runs=[
            _pick(
                ModelCheckRun,
                {
                    **run,
                    "app_slug": _nested_str(run.get("app"), "slug") or None,
                    "head_branch": _nested_str(run.get("check_suite"), "head_branch")
                    or None,
                },
            )
            for run in raw
        ],
    )


def read_merged_pr(
    transport: GitHubReadTransportProtocol, request: ModelGithubGatewayRequest
) -> ModelMergedPrResult:
    """PR state, head refs and actual merge details from REST (OMN-21146)."""
    pr_number = _require(request.pr_number, "pr_number")
    pr_request = pull_request_request(request.repo, pr_number)
    pr = _checked(pr_request, transport.send_sync(pr_request))
    merged = bool(pr.get("merged", False))
    merge_sha = pr.get("merge_commit_sha")
    if merged and not isinstance(merge_sha, str):
        # OMN-19528: REST leaves the key out of a merged PR on the deployed
        # lane; GraphQL still names the merge commit.
        merge_sha = transport.fetch_pr_detail(request.repo, pr_number).get(
            "mergeCommitOid"
        )
    merged_at = pr.get("merged_at")
    return ModelMergedPrResult(
        repo=request.repo,
        pr_number=pr_number,
        state="MERGED" if merged else str(pr.get("state", "")).upper(),
        merged=merged,
        head_sha=_nested_str(pr.get("head"), "sha"),
        head_ref=_nested_str(pr.get("head"), "ref"),
        base_ref=_nested_str(pr.get("base"), "ref"),
        # GitHub also supplies a test merge SHA for an unmerged PR.
        merge_commit_sha=merge_sha if merged and isinstance(merge_sha, str) else None,
        merged_at=merged_at if isinstance(merged_at, str) else None,
    )


def read_pr_review_threads(
    transport: GitHubReadTransportProtocol, request: ModelGithubGatewayRequest
) -> ModelPrReviewThreadsResult:
    """A PR's review threads with ids, path, line and resolved state."""
    pr_number = _require(request.pr_number, "pr_number")
    threads: list[ModelPrReviewThread] = []
    total: int | None = None
    after: str | None = None
    truncated = False
    for _page in range(_MAX_PAGES):
        page_request = review_threads_request(request.repo, pr_number, after=after)
        body = _checked(page_request, transport.send_sync(page_request))
        errors = body.get("errors")
        if errors:
            raise GitHubTransportError(
                f"GitHub GraphQL review threads of {request.repo}#{pr_number}: {errors}"
            )
        pull = _dict_at(body, "data", "repository", "pullRequest")
        if pull is None:
            raise GitHubTransportError(
                f"GitHub GraphQL found no pull request {request.repo}#{pr_number}"
            )
        connection = pull.get("reviewThreads")
        connection = connection if isinstance(connection, dict) else {}
        raw_total = connection.get("totalCount")
        if isinstance(raw_total, int):
            total = raw_total
        nodes = connection.get("nodes")
        for node in nodes if isinstance(nodes, list) else []:
            thread = _review_thread(node)
            if thread is None:
                continue
            if len(threads) == request.limit:
                truncated = True
                break
            threads.append(thread)
        page_info = connection.get("pageInfo")
        cursor = page_info.get("endCursor") if isinstance(page_info, dict) else None
        more = isinstance(page_info, dict) and bool(page_info.get("hasNextPage"))
        if truncated or len(threads) >= request.limit:
            truncated = truncated or more
            break
        if not more or not isinstance(cursor, str):
            break
        after = cursor
    else:
        truncated = True
    return ModelPrReviewThreadsResult(
        repo=request.repo,
        pr_number=pr_number,
        total_count=total,
        truncated=truncated,
        threads=threads,
    )


def _dict_at(raw: object, *path: str) -> dict[str, object] | None:
    node = raw
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, dict) else None


def _review_thread(node: object) -> ModelPrReviewThread | None:
    """One GraphQL thread node; None for a thread with no comment left."""
    if not isinstance(node, dict):
        return None
    block = node.get("comments")
    block = block if isinstance(block, dict) else {}
    raw_comments = block.get("nodes")
    comments: list[ModelPrReviewThreadComment] = []
    for raw in raw_comments if isinstance(raw_comments, list) else []:
        if not isinstance(raw, dict) or not isinstance(raw.get("databaseId"), int):
            continue
        text, cut = _cut(raw.get("body"))
        comments.append(
            ModelPrReviewThreadComment(
                comment_id=raw["databaseId"],
                author=_login(raw.get("author")),
                created_at=str(raw.get("createdAt", "")),
                url=str(raw.get("url", "")),
                body=text,
                body_truncated=cut,
            )
        )
    if not comments:
        return None
    count = block.get("totalCount")
    line = node.get("line")
    path = node.get("path")
    return ModelPrReviewThread(
        thread_id=str(node.get("id", "")),
        is_resolved=bool(node.get("isResolved", False)),
        is_outdated=bool(node.get("isOutdated", False)),
        path=path if isinstance(path, str) else None,
        line=line if isinstance(line, int) else None,
        root_comment_id=comments[0].comment_id,
        comment_count=count if isinstance(count, int) else len(comments),
        comments=comments,
    )


def _require(value: int | None, name: str) -> int:
    if value is None:  # defensive; the request validator already enforces this
        raise ValueError(f"this operation requires {name}.")
    return value


__all__: list[str] = [
    "read_artifacts",
    "read_branch_protection",
    "read_ci_checks",
    "read_commit_check_runs",
    "read_job_log_tail",
    "read_merge_commit_sha",
    "read_merged_pr",
    "read_open_prs_list",
    "read_pr_heads",
    "read_pr_review_threads",
    "read_pr_status",
    "read_pr_text",
    "read_releases",
    "read_review_gate",
    "read_run_jobs",
    "read_ticket_ref",
    "read_workflow_runs",
]

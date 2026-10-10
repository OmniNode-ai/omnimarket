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
from typing import Any, Literal

from pydantic import BaseModel

from omnimarket.github_landing.github_landing_requests import (
    LIST_PAGE_SIZE,
    artifact_archive_request,
    artifact_request,
    issue_comments_request,
    job_log_request,
    next_page_request,
    pull_request_request,
    releases_request,
    run_artifacts_request,
    run_jobs_request,
    tags_request,
    workflow_runs_request,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.nodes.node_github_repo_gateway_effect.models.model_gateway_io import (
    MAX_TEXT_CHARS,
    EnumArtifactFetchRefusal,
    ModelArtifactContent,
    ModelArtifactsResult,
    ModelArtifactSummary,
    ModelBranchProtectionResult,
    ModelCiChecksResult,
    ModelGithubGatewayRequest,
    ModelJobLogTailResult,
    ModelJobStep,
    ModelMergeCommitShaResult,
    ModelOpenPrsResult,
    ModelOpenPrSummary,
    ModelPrComment,
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
    """The last ``tail_bytes`` of one job's log."""
    job_id = _require(request.job_id, "job_id")
    log_request = job_log_request(request.repo, job_id)
    response = transport.send_bytes_sync(
        log_request, limit=request.tail_bytes, keep="tail"
    )
    if not 200 <= response.status < 300:
        raise GitHubTransportError(
            f"GitHub GET {log_request.path} answered HTTP {response.status}: "
            f"{response.content.decode('utf-8', errors='replace')}"
        )
    return ModelJobLogTailResult(
        repo=request.repo,
        job_id=job_id,
        total_bytes=response.total_bytes,
        truncated=response.over_limit,
        text=response.content.decode("utf-8", errors="replace"),
    )


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


def _require(value: int | None, name: str) -> int:
    if value is None:  # defensive; the request validator already enforces this
        raise ValueError(f"this operation requires {name}.")
    return value


__all__: list[str] = [
    "read_artifacts",
    "read_branch_protection",
    "read_ci_checks",
    "read_job_log_tail",
    "read_merge_commit_sha",
    "read_open_prs_list",
    "read_pr_status",
    "read_pr_text",
    "read_releases",
    "read_review_gate",
    "read_run_jobs",
    "read_ticket_ref",
    "read_workflow_runs",
]

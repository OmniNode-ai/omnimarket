# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure functions: verify a GitHub webhook signature, fold a delivery (OMN-19492).

Nothing here reads the environment, the clock or the network. The handler
supplies the secret, the received time and the publish time, so every function
is deterministic and replayable from a recorded delivery.

What each GitHub event contributes (``None`` = "says nothing"):

- ``pull_request``: title, draft, base/head ref, head sha, mergeable and
  mergeable_state; triage_state on open, ready, draft and close; ci_status
  PENDING on a new head (opened, reopened, synchronize), so a verdict from an
  older head is never served against a new one; merge_queue_state on
  enqueued, dequeued, auto_merge_* and merged. A merged close also yields a
  pr-merged event.
- ``pull_request_review``: review_decision, APPROVED or CHANGES_REQUESTED on
  submit and REVIEW_REQUIRED on dismiss; a plain comment says nothing.
- ``check_run``: ci_status for each PR the run names, but only when the run is
  one of the configured summary checks (the one required context per repo,
  e.g. "CI Summary"); a lone check run is not a PR's verdict. A summary run
  on a watched branch also yields branch-head-status; a run on
  gh-readonly-queue/<watched>/... yields merge-group-status instead, never a
  branch-head verdict.
  Any check run, summary or not, that completes red also yields one check-run
  observation per PR the run names (OMN-20743): its conclusion, workflow run
  id (parsed from ``details_url``; none for a check another app posted) and
  completion time. A red is a conclusion in ``RED_CHECK_CONCLUSIONS``, the set
  a PR-state watcher counts; a cancelled run has no verdict.
- ``workflow_run``: one workflow-run observation carrying the run id and the
  workflow's name, which no check-run delivery names (OMN-20743).
- ``push``: branch-ref-advanced for a watched refs/heads/<branch>, excluding
  deletions. Branch observations are published on the branch-head topic.
- anything else (ping, check_suite, status, ...): nothing.

Known gap, owned by the reconciler (OMN-19493): deliveries can arrive out of
order and a check run can report on a head the PR has already moved past. The
writer keeps the newest stored value per column only by arrival order, and the
reconciler's conditional read heals a regressed row.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from omnimarket.nodes.node_github_webhook_ingress_effect.models import (
    ModelGitHubBranchHeadObservation,
    ModelGitHubCheckRunObservation,
    ModelGitHubPrMergedObservation,
    ModelGitHubPrStateObservation,
    ModelGitHubWorkflowRunObservation,
)

_SIGNATURE_PREFIX = "sha256="
_TICKET_RE = re.compile(r"\b(OMN-\d+)\b", re.IGNORECASE)
_MERGED_EVENT_NAMESPACE = uuid.UUID("5b0d6c1e-8f3a-4f5e-9c2d-14375a192490")
_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")
# The conclusions a PR-state watcher counts as red. A cancelled run has no verdict.
RED_CHECK_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "action_required", "startup_failure", "stale"}
)
_WORKFLOW_RUN_STATUSES: Mapping[
    str, Literal["requested", "in_progress", "completed"]
] = {
    "requested": "requested",
    "in_progress": "in_progress",
    "completed": "completed",
}
_NEW_HEAD_ACTIONS = frozenset({"opened", "reopened", "synchronize"})
_QUEUE_ACTIONS: Mapping[str, str] = {
    "enqueued": "QUEUED",
    "dequeued": "DEQUEUED",
    "auto_merge_enabled": "AUTO_MERGE_ARMED",
    "auto_merge_disabled": "AUTO_MERGE_DISARMED",
}
_REVIEW_STATES: Mapping[str, str] = {
    "approved": "APPROVED",
    "changes_requested": "CHANGES_REQUESTED",
}


class WebhookFoldError(ValueError):
    """A delivery whose body is not the shape GitHub documents for its event."""


def verify_signature(secret: bytes, body: bytes, signature_header: str) -> bool:
    """True when ``signature_header`` is GitHub's HMAC-SHA256 of ``body``.

    Constant-time comparison. An empty secret never verifies anything.
    """
    if not secret or not signature_header.startswith(_SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len(_SIGNATURE_PREFIX) :])


def fold_delivery(
    *,
    event: str,
    delivery_id: UUID,
    payload: Mapping[str, object],
    received_at: datetime,
    published_at: datetime,
    summary_check_names: frozenset[str],
    watched_branches: frozenset[str] = frozenset(),
) -> tuple[BaseModel, ...]:
    """Fold one verified delivery into the observations it implies."""
    if event == "pull_request":
        return _fold_pull_request(delivery_id, payload, received_at, published_at)
    if event == "pull_request_review":
        return _fold_review(delivery_id, payload, received_at)
    if event == "check_run":
        return _fold_check_run(
            delivery_id, payload, received_at, summary_check_names, watched_branches
        )
    if event == "workflow_run":
        return _fold_workflow_run(delivery_id, payload, received_at)
    if event == "push":
        return _fold_push(delivery_id, payload, received_at, watched_branches)
    return ()


def _fold_workflow_run(
    delivery_id: UUID, payload: Mapping[str, object], received_at: datetime
) -> tuple[BaseModel, ...]:
    run = _mapping(payload.get("workflow_run"), "workflow_run")
    name = _str(run.get("name"))
    head_sha = _str(run.get("head_sha"))
    status = _WORKFLOW_RUN_STATUSES.get(_str(payload.get("action")) or "")
    if name is None or head_sha is None or status is None:
        return ()
    repo = _repo(payload)
    run_id = _int(run.get("id"), "workflow_run.id")
    return (
        ModelGitHubWorkflowRunObservation(
            repo=repo,
            run_id=run_id,
            workflow=name,
            head_sha=head_sha,
            status=status,
            delivery_id=delivery_id,
            github_event="workflow_run",
            as_of=_time(run.get("updated_at")) or received_at,
        ),
    )


def _fold_push(
    delivery_id: UUID,
    payload: Mapping[str, object],
    received_at: datetime,
    watched_branches: frozenset[str],
) -> tuple[BaseModel, ...]:
    ref = _str(payload.get("ref"))
    if not ref or not ref.startswith("refs/heads/") or payload.get("deleted") is True:
        return ()
    branch = ref.removeprefix("refs/heads/")
    if branch not in watched_branches:
        return ()
    repo = _repo(payload)
    sha = _str(payload.get("after"))
    if sha is None:
        raise WebhookFoldError("push.after missing or not a non-empty string")
    return (
        ModelGitHubBranchHeadObservation(
            kind="branch-ref-advanced",
            entity_id=f"{repo}@{branch}",
            repo=repo,
            branch=branch,
            delivery_id=delivery_id,
            github_event="push",
            as_of=received_at,
            sha=sha,
            before_sha=_str(payload.get("before")),
        ),
    )


def _fold_pull_request(
    delivery_id: UUID,
    payload: Mapping[str, object],
    received_at: datetime,
    published_at: datetime,
) -> tuple[BaseModel, ...]:
    action = _str(payload.get("action"))
    pr = _mapping(payload.get("pull_request"), "pull_request")
    repo = _repo(payload)
    number = _int(pr.get("number"), "pull_request.number")
    head = _mapping(pr.get("head"), "pull_request.head")
    base = _mapping(pr.get("base"), "pull_request.base")
    closed = pr.get("state") == "closed"
    merged = closed and pr.get("merged") is True
    draft = pr.get("draft") if isinstance(pr.get("draft"), bool) else None

    triage: str | None = None
    if closed:
        triage = "merged" if merged else "closed"
    elif draft is True:
        triage = "draft"
    elif action in {"opened", "reopened", "ready_for_review"}:
        triage = "needs_review"

    queue: str | None = _QUEUE_ACTIONS.get(action or "")
    if merged:
        queue = "MERGED"

    mergeable_raw = pr.get("mergeable")
    mergeable = (
        "MERGEABLE"
        if mergeable_raw is True
        else "CONFLICTING"
        if mergeable_raw is False
        else None
    )
    merge_state = _str(pr.get("mergeable_state"))
    as_of = (
        _time(pr.get("closed_at") if closed else pr.get("updated_at")) or received_at
    )

    observations: list[BaseModel] = [
        ModelGitHubPrStateObservation(
            entity_id=f"{repo}#{number}",
            repo=repo,
            pr_number=number,
            delivery_id=delivery_id,
            github_event="pull_request",
            as_of=as_of,
            triage_state=triage,
            title=_str(pr.get("title")),
            is_draft=draft,
            ci_status="PENDING" if action in _NEW_HEAD_ACTIONS and not closed else None,
            mergeable=mergeable,
            merge_state_status=(
                merge_state.upper()
                if merge_state and merge_state != "unknown"
                else None
            ),
            merge_queue_state=queue,
            base_ref=_str(base.get("ref")),
            head_ref=_str(head.get("ref")),
            head_sha=_str(head.get("sha")),
        )
    ]
    if merged and action == "closed":
        observations.append(_merged(repo, pr, published_at))
    return tuple(observations)


def _merged(
    repo: str,
    pr: Mapping[str, object],
    published_at: datetime,
) -> ModelGitHubPrMergedObservation:
    number = _int(pr.get("number"), "pull_request.number")
    head = _mapping(pr.get("head"), "pull_request.head")
    base = _mapping(pr.get("base"), "pull_request.base")
    merge_sha = _str(pr.get("merge_commit_sha"))
    merged_at = _str(pr.get("merged_at"))
    branch = _str(head.get("ref"))
    base_ref = _str(base.get("ref"))
    if not merge_sha or not merged_at or not branch or not base_ref:
        raise WebhookFoldError(
            "merged pull_request lacks merge_commit_sha, merged_at or refs"
        )
    ticket = ""
    for text in (branch, _str(pr.get("title")) or ""):
        found = _TICKET_RE.search(text)
        if found:
            ticket = found.group(1).upper()
            break
    return ModelGitHubPrMergedObservation(
        entity_id=f"{repo}#{number}",
        event_id=uuid.uuid5(_MERGED_EVENT_NAMESPACE, f"{repo}#{number}@{merge_sha}"),
        repo=repo,
        branch=branch,
        base_ref=base_ref,
        pr_number=number,
        ticket=ticket,
        merge_sha=merge_sha,
        merged_at=merged_at,
        published_at=published_at.astimezone(UTC).isoformat(),
    )


def _fold_review(
    delivery_id: UUID, payload: Mapping[str, object], received_at: datetime
) -> tuple[BaseModel, ...]:
    action = _str(payload.get("action"))
    review = _mapping(payload.get("review"), "review")
    pr = _mapping(payload.get("pull_request"), "pull_request")
    if action == "dismissed":
        decision: str | None = "REVIEW_REQUIRED"
    elif action == "submitted":
        decision = _REVIEW_STATES.get((_str(review.get("state")) or "").lower())
    else:
        decision = None
    if decision is None:
        return ()
    repo = _repo(payload)
    number = _int(pr.get("number"), "pull_request.number")
    return (
        ModelGitHubPrStateObservation(
            entity_id=f"{repo}#{number}",
            repo=repo,
            pr_number=number,
            delivery_id=delivery_id,
            github_event="pull_request_review",
            as_of=_time(review.get("submitted_at")) or received_at,
            review_decision=decision,
        ),
    )


def _fold_check_run(
    delivery_id: UUID,
    payload: Mapping[str, object],
    received_at: datetime,
    summary_check_names: frozenset[str],
    watched_branches: frozenset[str],
) -> tuple[BaseModel, ...]:
    run = _mapping(payload.get("check_run"), "check_run")
    name = _str(run.get("name"))
    status = _str(run.get("status"))
    conclusion = _str(run.get("conclusion"))
    completed_at = _time(run.get("completed_at"))
    red = (
        status == "completed"
        and conclusion is not None
        and conclusion.lower() in RED_CHECK_CONCLUSIONS
        and name is not None
        and completed_at is not None
    )
    if not red and name not in summary_check_names:
        return ()
    repo = _repo(payload)
    as_of = completed_at or _time(run.get("started_at")) or received_at
    prs = run.get("pull_requests")
    out: list[BaseModel] = []
    if red and name is not None and completed_at is not None:
        out.extend(_red_check_runs(delivery_id, repo, run, completed_at))
    if name not in summary_check_names:
        return tuple(out)
    verdict = conclusion.upper() if status == "completed" and conclusion else "PENDING"
    for item in prs if isinstance(prs, list) else []:
        if not isinstance(item, Mapping):
            continue
        number = item.get("number")
        if not isinstance(number, int) or number < 1:
            continue
        out.append(
            ModelGitHubPrStateObservation(
                entity_id=f"{repo}#{number}",
                repo=repo,
                pr_number=number,
                delivery_id=delivery_id,
                github_event="check_run",
                as_of=as_of,
                ci_status=verdict,
                head_sha=_str(run.get("head_sha")),
            )
        )
    suite = run.get("check_suite")
    head_branch = _str(suite.get("head_branch")) if isinstance(suite, Mapping) else None
    branch = None
    merge_group_ref = None
    if head_branch and head_branch.startswith("gh-readonly-queue/"):
        # Longest first also handles watched branch names containing slashes.
        for watched in sorted(watched_branches, key=lambda name: (-len(name), name)):
            prefix = f"gh-readonly-queue/{watched}/"
            if head_branch.startswith(prefix) and head_branch[len(prefix) :]:
                branch = watched
                merge_group_ref = head_branch
                break
    elif head_branch in watched_branches:
        branch = head_branch
    if branch is not None:
        sha = _str(run.get("head_sha"))
        if sha is None:
            raise WebhookFoldError(
                "check_run.head_sha missing or not a non-empty string"
            )
        out.append(
            ModelGitHubBranchHeadObservation(
                kind="merge-group-status" if merge_group_ref else "branch-head-status",
                entity_id=f"{repo}@{branch}",
                repo=repo,
                branch=branch,
                delivery_id=delivery_id,
                github_event="check_run",
                as_of=as_of,
                sha=sha,
                ci_status=verdict,
                check_name=_str(run.get("name")),
                merge_group_ref=merge_group_ref,
            )
        )
    return tuple(out)


def _red_check_runs(
    delivery_id: UUID, repo: str, run: Mapping[str, object], completed_at: datetime
) -> list[BaseModel]:
    head_sha = _str(run.get("head_sha"))
    if head_sha is None:
        raise WebhookFoldError("check_run.head_sha missing or not a non-empty string")
    found = _RUN_ID_RE.search(_str(run.get("details_url")) or "")
    prs = run.get("pull_requests")
    observations: list[BaseModel] = []
    for item in prs if isinstance(prs, list) else []:
        if not isinstance(item, Mapping):
            continue
        number = item.get("number")
        if not isinstance(number, int) or isinstance(number, bool) or number < 1:
            continue
        base = item.get("base")
        observations.append(
            ModelGitHubCheckRunObservation(
                repo=repo,
                pr_number=number,
                delivery_id=delivery_id,
                github_event="check_run",
                as_of=completed_at,
                head_sha=head_sha,
                base_ref=_str(base.get("ref")) if isinstance(base, Mapping) else None,
                check=_str(run.get("name")) or "",
                conclusion=(_str(run.get("conclusion")) or "").lower(),
                run_id=int(found.group(1)) if found else None,
                completed_at=completed_at,
            )
        )
    return observations


def _repo(payload: Mapping[str, object]) -> str:
    repository = _mapping(payload.get("repository"), "repository")
    name = _str(repository.get("full_name"))
    if not name or "/" not in name:
        raise WebhookFoldError("repository.full_name missing or not '<owner>/<repo>'")
    return name


def _mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WebhookFoldError(f"{where} missing or not an object")
    return value


def _int(value: object, where: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise WebhookFoldError(f"{where} missing or not a positive integer")
    return value


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


__all__: list[str] = [
    "RED_CHECK_CONCLUSIONS",
    "WebhookFoldError",
    "fold_delivery",
    "verify_signature",
]

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every GitHub request the landing paths send, as pure builders (OMN-19831).

One place for the REST paths and GraphQL documents that
node_pr_landing_github_effect, node_ci_rerun_effect,
node_merge_sweep_auto_merge_arm_effect and node_pr_lifecycle_fix_effect's
auto-rebase send. Each builder returns a :class:`ModelGithubHttpRequest`; none
of them performs I/O or touches a credential.
"""

from __future__ import annotations

import re
from typing import Literal

from omnimarket.github_landing.model_github_http_exchange import (
    GITHUB_GRAPHQL_PATH,
    ModelGithubHttpRequest,
)

_REPO_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")

# Page size of every list read. A page shorter than this is the last page.
LIST_PAGE_SIZE = 100

MergeMethod = Literal["SQUASH", "MERGE", "REBASE"]

# node_merge_sweep_auto_merge_arm_effect's mutation, byte for byte. It carries
# no expected head, so it is the legacy arm surface's shape only.
ENABLE_AUTO_MERGE_MUTATION = (
    "mutation($id: ID!, $method: PullRequestMergeMethod!) {"
    "  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) {"
    "    pullRequest { number }"
    "  }"
    "}"
)
# The landing effect's arm: GitHub refuses it once the head has moved past
# expectedHeadOid (plan revision 1, R4).
ENABLE_AUTO_MERGE_AT_HEAD_MUTATION = (
    "mutation($id: ID!, $method: PullRequestMergeMethod!, $head: GitObjectID!) {"
    " enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method,"
    " expectedHeadOid: $head}) { pullRequest { number } } }"
)
ENQUEUE_AT_HEAD_MUTATION = (
    "mutation($id: ID!, $head: GitObjectID!) {"
    " enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $head})"
    " { mergeQueueEntry { position } } }"
)
# Mirrors node_pr_lifecycle_merge_effect adapter_github_merge_queue's dequeue.
DEQUEUE_MUTATION = (
    "mutation($id: ID!) { dequeuePullRequest(input: {id: $id}) { clientMutationId } }"
)
# GitHub's documented disablePullRequestAutoMerge mutation.
DISABLE_AUTO_MERGE_MUTATION = (
    "mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) "
    "{ pullRequest { number } } }"
)
# The read an arm or enqueue makes first, in one call: the PR's head, draft
# flag, title and labels (the hold markers), open state, and the repository's
# live merge policy (auto-merge allowed, merge queue on the base branch).
LANDING_POLICY_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) { autoMergeAllowed"
    " pullRequest(number: $number) { id number headRefOid baseRefName isDraft"
    " title state merged isMergeQueueEnabled isInMergeQueue"
    " labels(first: 100) { nodes { name } } autoMergeRequest { mergeMethod } } } }"
)


class GithubLandingRequestError(ValueError):
    """A builder was given an input no GitHub request can be built from."""


def split_repository(repository: str) -> tuple[str, str]:
    """Return (owner, name) of an ``owner/name`` slug, refusing anything else."""
    if not _REPO_SLUG.match(repository):
        raise GithubLandingRequestError(
            f"repository must be owner/name, got {repository!r}"
        )
    owner, _, name = repository.partition("/")
    return owner, name


def _repo_path(repository: str) -> str:
    owner, name = split_repository(repository)
    return f"/repos/{owner}/{name}"


def _graphql(query: str, variables: dict[str, object]) -> ModelGithubHttpRequest:
    return ModelGithubHttpRequest(
        method="POST",
        path=GITHUB_GRAPHQL_PATH,
        body={"query": query, "variables": variables},
    )


# --- REST: runs and jobs ------------------------------------------------------


def rerun_failed_jobs_request(repository: str, run_id: int) -> ModelGithubHttpRequest:
    """POST rerun-failed-jobs for one workflow run."""
    return ModelGithubHttpRequest(
        method="POST",
        path=f"{_repo_path(repository)}/actions/runs/{run_id}/rerun-failed-jobs",
    )


def workflow_run_request(repository: str, run_id: int) -> ModelGithubHttpRequest:
    """GET one workflow run (its ``run_attempt``)."""
    return ModelGithubHttpRequest(
        method="GET", path=f"{_repo_path(repository)}/actions/runs/{run_id}"
    )


def run_jobs_request(
    repository: str, run_id: int, *, page: int = 1
) -> ModelGithubHttpRequest:
    """GET every job of a run, across all attempts (each with its run_attempt)."""
    return ModelGithubHttpRequest(
        method="GET",
        path=(
            f"{_repo_path(repository)}/actions/runs/{run_id}/jobs"
            f"?filter=all&per_page={LIST_PAGE_SIZE}&page={page}"
        ),
    )


# --- REST: pull requests and checks ------------------------------------------


def update_branch_request(
    repository: str, pr_number: int, expected_head_sha: str
) -> ModelGithubHttpRequest:
    """PUT update-branch, refused by GitHub once the head has moved."""
    return ModelGithubHttpRequest(
        method="PUT",
        path=f"{_repo_path(repository)}/pulls/{pr_number}/update-branch",
        body={"expected_head_sha": expected_head_sha},
    )


def pull_request_request(
    repository: str, pr_number: int, *, etag: str | None = None
) -> ModelGithubHttpRequest:
    """GET one pull request; conditional when an ETag is given (a 304 is free)."""
    return ModelGithubHttpRequest(
        method="GET",
        path=f"{_repo_path(repository)}/pulls/{pr_number}",
        if_none_match=etag,
    )


def head_check_runs_request(
    repository: str, head_sha: str, *, etag: str | None = None, page: int = 1
) -> ModelGithubHttpRequest:
    """GET the head's check runs; page 1 is conditional when an ETag is given."""
    return ModelGithubHttpRequest(
        method="GET",
        path=(
            f"{_repo_path(repository)}/commits/{head_sha}/check-runs"
            f"?filter=all&per_page={LIST_PAGE_SIZE}&page={page}"
        ),
        if_none_match=etag,
    )


# --- REST: git data (node_ci_rerun_effect's empty-commit re-trigger) ----------


def git_ref_request(repository: str, branch: str) -> ModelGithubHttpRequest:
    return ModelGithubHttpRequest(
        method="GET", path=f"{_repo_path(repository)}/git/ref/heads/{branch}"
    )


def git_commit_request(repository: str, sha: str) -> ModelGithubHttpRequest:
    return ModelGithubHttpRequest(
        method="GET", path=f"{_repo_path(repository)}/git/commits/{sha}"
    )


def create_commit_request(
    repository: str, *, message: str, tree_sha: str, parent_sha: str
) -> ModelGithubHttpRequest:
    return ModelGithubHttpRequest(
        method="POST",
        path=f"{_repo_path(repository)}/git/commits",
        body={"message": message, "tree": tree_sha, "parents": [parent_sha]},
    )


def fast_forward_ref_request(
    repository: str, branch: str, sha: str
) -> ModelGithubHttpRequest:
    return ModelGithubHttpRequest(
        method="PATCH",
        path=f"{_repo_path(repository)}/git/refs/heads/{branch}",
        body={"sha": sha, "force": False},
    )


# --- GraphQL ------------------------------------------------------------------


def landing_policy_request(repository: str, pr_number: int) -> ModelGithubHttpRequest:
    """The one read before an arm or enqueue: PR state and live merge policy."""
    owner, name = split_repository(repository)
    return _graphql(
        LANDING_POLICY_QUERY, {"owner": owner, "name": name, "number": pr_number}
    )


def enable_auto_merge_request(
    pr_node_id: str, merge_method: MergeMethod, *, expected_head_sha: str | None
) -> ModelGithubHttpRequest:
    """Arm auto-merge. With an expected head, GitHub refuses it on a moved head."""
    if expected_head_sha is None:
        return _graphql(
            ENABLE_AUTO_MERGE_MUTATION, {"id": pr_node_id, "method": merge_method}
        )
    return _graphql(
        ENABLE_AUTO_MERGE_AT_HEAD_MUTATION,
        {"id": pr_node_id, "method": merge_method, "head": expected_head_sha},
    )


def enqueue_request(pr_node_id: str, expected_head_sha: str) -> ModelGithubHttpRequest:
    """Enqueue in the base branch's merge queue at the expected head."""
    return _graphql(
        ENQUEUE_AT_HEAD_MUTATION, {"id": pr_node_id, "head": expected_head_sha}
    )


def disable_auto_merge_request(pr_node_id: str) -> ModelGithubHttpRequest:
    return _graphql(DISABLE_AUTO_MERGE_MUTATION, {"id": pr_node_id})


def dequeue_request(pr_node_id: str) -> ModelGithubHttpRequest:
    return _graphql(DEQUEUE_MUTATION, {"id": pr_node_id})


__all__: list[str] = [
    "DEQUEUE_MUTATION",
    "DISABLE_AUTO_MERGE_MUTATION",
    "ENABLE_AUTO_MERGE_AT_HEAD_MUTATION",
    "ENABLE_AUTO_MERGE_MUTATION",
    "ENQUEUE_AT_HEAD_MUTATION",
    "LANDING_POLICY_QUERY",
    "LIST_PAGE_SIZE",
    "GithubLandingRequestError",
    "MergeMethod",
    "create_commit_request",
    "dequeue_request",
    "disable_auto_merge_request",
    "enable_auto_merge_request",
    "enqueue_request",
    "fast_forward_ref_request",
    "git_commit_request",
    "git_ref_request",
    "head_check_runs_request",
    "landing_policy_request",
    "pull_request_request",
    "rerun_failed_jobs_request",
    "run_jobs_request",
    "split_repository",
    "update_branch_request",
    "workflow_run_request",
]

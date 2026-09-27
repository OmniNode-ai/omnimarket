# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command model of node_pr_landing_github_effect (OMN-19826).

``to_http_requests`` is the request-shape seam. Each shape mirrors the call
site already in use in this repository, so the wave-2 handlers and the old
nodes can share one transport:

- rerun_runs: node_ci_rerun_effect (POST .../actions/runs/{id}/rerun-failed-jobs)
- update_branch: node_pr_lifecycle_fix_effect handler_auto_rebase
  (PUT .../pulls/{n}/update-branch with expected_head_sha)
- arm_auto_merge: node_merge_sweep_auto_merge_arm_effect (enablePullRequestAutoMerge)
- enqueue and the queue form of disarm: node_pr_lifecycle_merge_effect
  adapter_github_merge_queue (enqueuePullRequest, dequeuePullRequest)
- read_head_checks: node_pr_lifecycle_inventory_compute
  (GET .../commits/{sha}/check-runs?filter=all&per_page=100&page=1)
- the auto-merge form of disarm has no existing call site; its shape is
  GitHub's documented disablePullRequestAutoMerge mutation.
- read_pr_state (OMN-19829, revision 1 of plan 5.1 section 6): GET
  .../pulls/{n}, conditional on the last ETag, the snapshot the landing
  orchestrator turns into its ordered observations.
"""

from __future__ import annotations

import re
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.nodes.node_pr_landing_github_effect.models.model_github_http_exchange import (
    GITHUB_GRAPHQL_PATH,
    ModelGithubHttpRequest,
)

_REPO_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# Mirrors node_merge_sweep_auto_merge_arm_effect's _GRAPHQL_MUTATION byte for byte.
ENABLE_AUTO_MERGE_MUTATION = (
    "mutation($id: ID!, $method: PullRequestMergeMethod!) {"
    "  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) {"
    "    pullRequest { number }"
    "  }"
    "}"
)
# Mirrors adapter_github_merge_queue's _ENQUEUE_MUTATION and _DEQUEUE_MUTATION.
ENQUEUE_MUTATION = (
    "mutation($id: ID!) { enqueuePullRequest(input: {pullRequestId: $id}) "
    "{ mergeQueueEntry { position } } }"
)
DEQUEUE_MUTATION = (
    "mutation($id: ID!) { dequeuePullRequest(input: {id: $id}) { clientMutationId } }"
)
# GitHub's documented disablePullRequestAutoMerge mutation.
DISABLE_AUTO_MERGE_MUTATION = (
    "mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) "
    "{ pullRequest { number } } }"
)

_CONDITIONAL_READS = frozenset(
    {
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        EnumPrLandingGithubOperation.READ_PR_STATE,
    }
)

_GRAPHQL_OPERATIONS = frozenset(
    {
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
        EnumPrLandingGithubOperation.ENQUEUE,
        EnumPrLandingGithubOperation.DISARM,
    }
)


class ModelPrLandingGithubRequest(BaseModel):
    """One GitHub operation the PR landing orchestrator asks the effect to run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    operation: EnumPrLandingGithubOperation
    mode: EnumPrLandingGithubMode
    repository: str = Field(description="owner/name")
    pr_number: int = Field(gt=0)
    head_sha: str | None = Field(
        default=None,
        description=(
            "The PR head the orchestrator observed. Required on every operation "
            "except read_pr_state, whose first read is how the head is learned."
        ),
    )
    run_ids: tuple[int, ...] = Field(
        default=(), description="rerun_runs only: the named workflow run ids."
    )
    pr_node_id: str | None = Field(
        default=None, description="GraphQL node id; arm_auto_merge, enqueue, disarm."
    )
    merge_method: Literal["SQUASH", "MERGE", "REBASE"] = "SQUASH"
    armed_method: Literal["auto_merge", "queue"] | None = Field(
        default=None, description="disarm only: how the PR was armed."
    )
    etag: str | None = Field(
        default=None,
        description=(
            "read_head_checks and read_pr_state only: the last ETag, for If-None-Match."
        ),
    )

    @field_validator("repository")
    @classmethod
    def _repository_is_a_slug(cls, value: str) -> str:
        if not _REPO_SLUG.match(value):
            raise ValueError(f"repository must be owner/name, got {value!r}")
        return value

    @field_validator("head_sha")
    @classmethod
    def _head_sha_is_full(cls, value: str | None) -> str | None:
        if value is not None and not _FULL_SHA.match(value):
            raise ValueError("head_sha must be a full 40-character lowercase sha")
        return value

    @model_validator(mode="after")
    def _fields_fit_the_operation(self) -> Self:
        op = self.operation
        if (
            self.head_sha is None
            and op is not EnumPrLandingGithubOperation.READ_PR_STATE
        ):
            raise ValueError(f"{op.value} requires the observed head_sha")
        if op is EnumPrLandingGithubOperation.RERUN_RUNS:
            if not self.run_ids:
                raise ValueError("rerun_runs requires named run_ids")
            if len(set(self.run_ids)) != len(self.run_ids) or min(self.run_ids) <= 0:
                raise ValueError("run_ids must be distinct positive run ids")
        elif self.run_ids:
            raise ValueError(f"run_ids is only valid on rerun_runs, not {op.value}")
        if op in _GRAPHQL_OPERATIONS:
            if not self.pr_node_id:
                raise ValueError(f"{op.value} requires pr_node_id")
        elif self.pr_node_id is not None:
            raise ValueError(f"pr_node_id is not used by {op.value}")
        if op is EnumPrLandingGithubOperation.DISARM:
            if self.armed_method is None:
                raise ValueError("disarm requires armed_method (auto_merge or queue)")
        elif self.armed_method is not None:
            raise ValueError(f"armed_method is only valid on disarm, not {op.value}")
        if self.etag is not None and op not in _CONDITIONAL_READS:
            raise ValueError(
                f"etag is only valid on read_head_checks and read_pr_state, not {op.value}"
            )
        return self

    def to_http_requests(self) -> tuple[ModelGithubHttpRequest, ...]:
        """The exact requests this command sends (enforce) or records (dry_run)."""
        repo = self.repository
        op = self.operation
        if op is EnumPrLandingGithubOperation.RERUN_RUNS:
            return tuple(
                ModelGithubHttpRequest(
                    method="POST",
                    path=f"/repos/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
                )
                for run_id in self.run_ids
            )
        if op is EnumPrLandingGithubOperation.UPDATE_BRANCH:
            return (
                ModelGithubHttpRequest(
                    method="PUT",
                    path=f"/repos/{repo}/pulls/{self.pr_number}/update-branch",
                    body={"expected_head_sha": self.head_sha},
                ),
            )
        if op is EnumPrLandingGithubOperation.READ_HEAD_CHECKS:
            return (
                ModelGithubHttpRequest(
                    method="GET",
                    path=(
                        f"/repos/{repo}/commits/{self.head_sha}/check-runs"
                        "?filter=all&per_page=100&page=1"
                    ),
                    if_none_match=self.etag,
                ),
            )
        if op is EnumPrLandingGithubOperation.READ_PR_STATE:
            return (
                ModelGithubHttpRequest(
                    method="GET",
                    path=f"/repos/{repo}/pulls/{self.pr_number}",
                    if_none_match=self.etag,
                ),
            )
        return (self._graphql_request(),)

    def _graphql_request(self) -> ModelGithubHttpRequest:
        node_id = self.pr_node_id
        variables: dict[str, object] = {"id": node_id}
        if self.operation is EnumPrLandingGithubOperation.ARM_AUTO_MERGE:
            query = ENABLE_AUTO_MERGE_MUTATION
            variables["method"] = self.merge_method
        elif self.operation is EnumPrLandingGithubOperation.ENQUEUE:
            query = ENQUEUE_MUTATION
        elif self.armed_method == "queue":
            query = DEQUEUE_MUTATION
        else:
            query = DISABLE_AUTO_MERGE_MUTATION
        return ModelGithubHttpRequest(
            method="POST",
            path=GITHUB_GRAPHQL_PATH,
            body={"query": query, "variables": variables},
        )

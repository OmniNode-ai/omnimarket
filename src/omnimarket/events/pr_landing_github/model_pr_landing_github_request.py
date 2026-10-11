# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command model of node_pr_landing_github_effect (OMN-19826, contract 1.1.0 by OMN-19831).

``to_http_requests`` returns the requests a command plans before any response
is read: what dry_run records and what enforce sends first. Every request is
built by ``omnimarket.github_landing.github_landing_requests``, the same
builders node_ci_rerun_effect, node_merge_sweep_auto_merge_arm_effect and the
fix effect's auto-rebase use, so all of them share one request shape.

Contract 1.1.0 (plan revision 1, R4 and section 6):

- ``read_pr_state`` reads the PR's head, draft flag, title, labels, state,
  merged and auto-merge state with a conditional GET (a 304 costs no quota).
  It is the one operation that may omit ``head_sha``: the autobind prompt that
  triggers the first read carries no head.
- ``arm_auto_merge`` and ``enqueue`` plan two requests: the landing policy
  read (head, draft, hold markers, open state and the repository's live merge
  policy, in one GraphQL call) and then the mutation, which carries
  ``head_sha`` as ``expectedHeadOid`` so GitHub itself refuses a moved head.
  The handler sends the mutation only when that read shows the PR open, not
  draft, not held, at ``head_sha`` and armable by the live policy.
- ``read_head_checks`` with ``base_ref`` (OMN-20866) also reads the base
  branch's required status contexts (classic protection and rulesets), so the
  head-check classifier knows which checks decide the verdict. Those reads
  depend on the check-runs answer (none follows a 304), so they are not planned.
"""

from __future__ import annotations

import re
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.events.pr_landing_github.enum_pr_landing_github_operation import (
    EnumPrLandingGithubOperation,
)
from omnimarket.github_landing.github_landing_requests import (
    dequeue_request,
    disable_auto_merge_request,
    enable_auto_merge_request,
    enqueue_request,
    head_check_runs_request,
    landing_policy_request,
    pull_request_request,
    rerun_failed_jobs_request,
    update_branch_request,
)
from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
)

_REPO_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

_GRAPHQL_OPERATIONS = frozenset(
    {
        EnumPrLandingGithubOperation.ARM_AUTO_MERGE,
        EnumPrLandingGithubOperation.ENQUEUE,
        EnumPrLandingGithubOperation.DISARM,
    }
)
_CONDITIONAL_READS = frozenset(
    {
        EnumPrLandingGithubOperation.READ_HEAD_CHECKS,
        EnumPrLandingGithubOperation.READ_PR_STATE,
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
            "The PR head the orchestrator observed; the expected head of an arm, "
            "enqueue or update-branch. Required by every operation except "
            "read_pr_state."
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
    base_ref: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "read_head_checks only: the PR's base branch, whose required status "
            "contexts the effect reads beside the check runs (OMN-20866)."
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
            raise ValueError(f"{op.value} requires head_sha")
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
        if (
            self.base_ref is not None
            and op is not EnumPrLandingGithubOperation.READ_HEAD_CHECKS
        ):
            raise ValueError(
                f"base_ref is only valid on read_head_checks, not {op.value}"
            )
        if self.etag is not None and op not in _CONDITIONAL_READS:
            raise ValueError(
                f"etag is only valid on read_head_checks and read_pr_state, not {op.value}"
            )
        return self

    def required_head_sha(self) -> str:
        """``head_sha``, which every operation but read_pr_state carries."""
        if self.head_sha is None:
            raise ValueError(f"{self.operation.value} has no head_sha")
        return self.head_sha

    def required_pr_node_id(self) -> str:
        if self.pr_node_id is None:
            raise ValueError(f"{self.operation.value} has no pr_node_id")
        return self.pr_node_id

    def to_http_requests(self) -> tuple[ModelGithubHttpRequest, ...]:
        """The requests this command plans before reading any response.

        dry_run records exactly these. enforce sends them in order; the
        handler's follow-up reads (a run's jobs for its attempts, a re-run
        read-back, the next check-runs page) depend on responses and are not
        planned here.
        """
        repo = self.repository
        op = self.operation
        if op is EnumPrLandingGithubOperation.RERUN_RUNS:
            return tuple(rerun_failed_jobs_request(repo, r) for r in self.run_ids)
        if op is EnumPrLandingGithubOperation.UPDATE_BRANCH:
            return (
                update_branch_request(repo, self.pr_number, self.required_head_sha()),
            )
        if op is EnumPrLandingGithubOperation.READ_HEAD_CHECKS:
            return (
                head_check_runs_request(repo, self.required_head_sha(), etag=self.etag),
            )
        if op is EnumPrLandingGithubOperation.READ_PR_STATE:
            return (pull_request_request(repo, self.pr_number, etag=self.etag),)
        node_id = self.required_pr_node_id()
        if op is EnumPrLandingGithubOperation.ARM_AUTO_MERGE:
            return (
                landing_policy_request(repo, self.pr_number),
                enable_auto_merge_request(
                    node_id,
                    self.merge_method,
                    expected_head_sha=self.required_head_sha(),
                ),
            )
        if op is EnumPrLandingGithubOperation.ENQUEUE:
            return (
                landing_policy_request(repo, self.pr_number),
                enqueue_request(node_id, self.required_head_sha()),
            )
        if self.armed_method == "queue":
            return (dequeue_request(node_id),)
        return (disable_auto_merge_request(node_id),)

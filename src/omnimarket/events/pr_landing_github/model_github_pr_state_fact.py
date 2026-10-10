# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A PR's landing state as GitHub reported it (OMN-19831, contract 1.1.0).

Facts only, from one response: ``read_pr_state`` fills it from the REST pull
request read, and an arm or enqueue fills it from the GraphQL read it makes
before the mutation, which adds the repository's live merge policy. Hold
markers are not judged here; the caller applies
``omnimarket.merge_control.hold_marker`` to ``title`` and ``labels``.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class GithubPrStateParseError(ValueError):
    """A pull request response did not carry a field the fact needs."""


def _require(mapping: object, key: str, context: str) -> object:
    if not isinstance(mapping, dict) or key not in mapping:
        raise GithubPrStateParseError(f"{context} has no {key!r}")
    return mapping[key]


def _require_str(mapping: object, key: str, context: str) -> str:
    value = _require(mapping, key, context)
    if not isinstance(value, str) or not value:
        raise GithubPrStateParseError(f"{context}.{key} is not a non-empty string")
    return value


def _require_bool(mapping: object, key: str, context: str) -> bool:
    value = _require(mapping, key, context)
    if not isinstance(value, bool):
        raise GithubPrStateParseError(f"{context}.{key} is not a boolean")
    return value


def _optional_str(mapping: dict[str, object], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None


def _head_sha(value: str, context: str) -> str:
    if not _FULL_SHA.match(value):
        raise GithubPrStateParseError(f"{context} head is not a full sha")
    return value


def _label_names(nodes: object, context: str) -> tuple[str, ...]:
    if not isinstance(nodes, list):
        raise GithubPrStateParseError(f"{context} labels is not a list")
    names: list[str] = []
    for node in nodes:
        names.append(_require_str(node, "name", f"{context} label"))
    return tuple(names)


class ModelGithubPrStateFact(BaseModel):
    """Head, draft flag, title, labels, open state, merged and auto-merge state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_node_id: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
    head_sha: str = Field(min_length=40, max_length=40)
    base_ref: str = Field(min_length=1)
    draft: bool
    title: str
    labels: tuple[str, ...]
    state: Literal["open", "closed"]
    merged: bool
    auto_merge_armed: bool
    auto_merge_method: str | None = Field(
        default=None, description="SQUASH, MERGE or REBASE when armed."
    )
    in_merge_queue: bool | None = Field(
        default=None, description="GraphQL read only; the REST read cannot see it."
    )
    merge_queue_enabled: bool | None = Field(
        default=None,
        description="GraphQL read only: the base branch has a merge queue.",
    )
    auto_merge_allowed: bool | None = Field(
        default=None,
        description="GraphQL read only: the repository allows auto-merge.",
    )
    mergeable_state: str | None = Field(
        default=None,
        description=(
            "REST read only: GitHub's mergeable_state (clean, blocked, behind, "
            "dirty, unstable, has_hooks, draft or unknown), as reported (OMN-20866)."
        ),
    )

    @classmethod
    def from_rest_pull(cls, body: dict[str, object] | None) -> ModelGithubPrStateFact:
        """Parse ``GET /repos/{owner}/{repo}/pulls/{number}``."""
        ctx = "pull request"
        if body is None:
            raise GithubPrStateParseError("pull request response has no body")
        head = _require(body, "head", ctx)
        base = _require(body, "base", ctx)
        state = _require_str(body, "state", ctx)
        if state not in ("open", "closed"):
            raise GithubPrStateParseError(
                f"{ctx}.state {state!r} is not open or closed"
            )
        auto_merge = body.get("auto_merge")
        method: str | None = None
        if isinstance(auto_merge, dict):
            raw_method = auto_merge.get("merge_method")
            method = raw_method.upper() if isinstance(raw_method, str) else None
        number = _require(body, "number", ctx)
        if not isinstance(number, int):
            raise GithubPrStateParseError(f"{ctx}.number is not an integer")
        return cls(
            pr_node_id=_require_str(body, "node_id", ctx),
            pr_number=number,
            head_sha=_head_sha(_require_str(head, "sha", f"{ctx}.head"), ctx),
            base_ref=_require_str(base, "ref", f"{ctx}.base"),
            draft=_require_bool(body, "draft", ctx),
            title=_require_str(body, "title", ctx),
            labels=_label_names(_require(body, "labels", ctx), ctx),
            state="open" if state == "open" else "closed",
            merged=_require_bool(body, "merged", ctx),
            auto_merge_armed=isinstance(auto_merge, dict),
            auto_merge_method=method,
            mergeable_state=_optional_str(body, "mergeable_state"),
        )

    @classmethod
    def from_policy_read(cls, body: dict[str, object] | None) -> ModelGithubPrStateFact:
        """Parse the GraphQL landing policy read (``LANDING_POLICY_QUERY``)."""
        ctx = "policy read"
        data = _require(body, "data", ctx)
        repository = _require(data, "repository", ctx)
        if repository is None:
            raise GithubPrStateParseError(f"{ctx} found no repository")
        pr = _require(repository, "pullRequest", f"{ctx}.repository")
        if pr is None:
            raise GithubPrStateParseError(f"{ctx} found no pull request")
        pctx = f"{ctx}.pullRequest"
        state = _require_str(pr, "state", pctx)
        request = _require(pr, "autoMergeRequest", pctx)
        method: str | None = None
        if isinstance(request, dict):
            raw_method = request.get("mergeMethod")
            method = raw_method if isinstance(raw_method, str) else None
        number = _require(pr, "number", pctx)
        if not isinstance(number, int):
            raise GithubPrStateParseError(f"{pctx}.number is not an integer")
        labels = _require(_require(pr, "labels", pctx), "nodes", f"{pctx}.labels")
        return cls(
            pr_node_id=_require_str(pr, "id", pctx),
            pr_number=number,
            head_sha=_head_sha(_require_str(pr, "headRefOid", pctx), pctx),
            base_ref=_require_str(pr, "baseRefName", pctx),
            draft=_require_bool(pr, "isDraft", pctx),
            title=_require_str(pr, "title", pctx),
            labels=_label_names(labels, pctx),
            # GraphQL states are OPEN, CLOSED and MERGED; a merged PR is closed.
            state="open" if state == "OPEN" else "closed",
            merged=_require_bool(pr, "merged", pctx),
            auto_merge_armed=isinstance(request, dict),
            auto_merge_method=method,
            in_merge_queue=_require_bool(pr, "isInMergeQueue", pctx),
            merge_queue_enabled=_require_bool(pr, "isMergeQueueEnabled", pctx),
            auto_merge_allowed=_require_bool(
                repository, "autoMergeAllowed", f"{ctx}.repository"
            ),
        )


__all__: list[str] = ["GithubPrStateParseError", "ModelGithubPrStateFact"]

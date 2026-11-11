# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A PR's landing state plus who merged it and the merge commit (OMN-23772).

The merge override alarm needs the merger and the merge commit, which only the
REST pull read reports. They live on this subclass of ``ModelGithubPrStateFact``
so the base model does not grow; every field here is optional and a reader of
the base model sees the same facts.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, Field

from omnimarket.events.pr_landing_github.model_github_pr_state_fact import (
    _FULL_SHA,
    GithubPrStateParseError,
    ModelGithubPrStateFact,
)


def _merged_by_login(body: dict[str, object]) -> str | None:
    """The REST ``merged_by.login``; None for a PR not merged or a deleted account."""
    merged_by = body.get("merged_by")
    if not isinstance(merged_by, dict):
        return None
    login = merged_by.get("login")
    return login if isinstance(login, str) and login else None


def _merge_commit_sha(body: dict[str, object], context: str) -> str | None:
    """The REST ``merge_commit_sha``; None when absent, an error when malformed."""
    value = body.get("merge_commit_sha")
    if value is None:
        return None
    if not isinstance(value, str) or not _FULL_SHA.match(value):
        raise GithubPrStateParseError(f"{context} merge_commit_sha is not a full sha")
    return value


class ModelGithubPrMergeStateFact(ModelGithubPrStateFact):
    """``ModelGithubPrStateFact`` with the merger and merge commit."""

    merged_by: str | None = Field(
        default=None,
        description=(
            "REST read only: the login of the account that merged the PR "
            "(merged_by.login); None when not merged or not reported."
        ),
    )
    merge_commit_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
        description="REST read only: the merge commit.",
    )

    @classmethod
    def from_rest_pull(
        cls, body: dict[str, object] | None
    ) -> ModelGithubPrMergeStateFact:
        base = super().from_rest_pull(body)
        if body is None:  # unreachable: the parent refused a missing body
            raise GithubPrStateParseError("pull request response has no body")
        return cls.model_validate(
            {
                **base.model_dump(),
                "merged_by": _merged_by_login(body),
                "merge_commit_sha": _merge_commit_sha(body, "pull request"),
            }
        )

    @classmethod
    def from_policy_read(
        cls, body: dict[str, object] | None
    ) -> ModelGithubPrMergeStateFact:
        return cls.model_validate(super().from_policy_read(body).model_dump())


def _widen(value: object) -> object:
    """Accept a plain ``ModelGithubPrStateFact`` as a merge state with no merger."""
    if isinstance(value, ModelGithubPrStateFact) and not isinstance(
        value, ModelGithubPrMergeStateFact
    ):
        return value.model_dump()
    return value


GithubPrMergeStateField = Annotated[
    ModelGithubPrMergeStateFact, BeforeValidator(_widen)
]

__all__: list[str] = ["GithubPrMergeStateField", "ModelGithubPrMergeStateFact"]

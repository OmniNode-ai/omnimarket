# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: every failed read is a typed result naming its cause (OMN-20669)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_remote_lane_effect.handlers import HandlerRemoteLaneRef
from omnimarket.nodes.node_remote_lane_effect.models import (
    EnumRemoteLaneRefRefusal,
    ModelRemoteLaneRefRequest,
)
from omnimarket.nodes.node_remote_lane_effect.protocols import (
    RemoteLaneCommandOutcome,
    RemoteLanePortError,
)

SHA = "a" * 40
HEAD_MAIN = RemoteLaneCommandOutcome(
    0, f"ref: refs/heads/main\tHEAD\n{SHA}\tHEAD\n", ""
)


class FakeGit:
    def __init__(
        self,
        outcomes: Sequence[RemoteLaneCommandOutcome | Exception],
        clones: frozenset[str] = frozenset(),
    ) -> None:
        self.outcomes = list(outcomes)
        self.clones = clones
        self.argvs: list[list[str]] = []

    def is_clone(self, path: str) -> bool:
        return path in self.clones

    def run(self, argv: Sequence[str], timeout_s: float) -> RemoteLaneCommandOutcome:
        self.argvs.append(list(argv))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _request(**fields: object) -> ModelRemoteLaneRefRequest:
    return ModelRemoteLaneRefRequest.model_validate(
        {"repo": "repo_a", "owner": "owner_a", **fields}
    )


@pytest.mark.parametrize(
    "fields",
    [{"repo": ""}, {"repo": "../x"}, {"owner": "a b"}, {"timeout_s": 0}, {"extra": 1}],
)
def test_malformed_requests_are_refused(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _request(**fields)


def test_a_malformed_sha_is_named_and_git_is_not_run() -> None:
    git = FakeGit([])
    result = HandlerRemoteLaneRef(git).handle(_request(sha="abc"))
    assert (result.sha, result.error, result.refusal) == (
        None,
        "the named sha is not a 40-character hex sha",
        EnumRemoteLaneRefRefusal.SHA_MALFORMED,
    )
    assert git.argvs == []


def test_without_a_clone_the_public_url_follows_every_option() -> None:
    git = FakeGit(
        [
            RemoteLaneCommandOutcome(
                0, f"ref: refs/heads/dev\tHEAD\n{SHA}\tHEAD\n", ""
            ),
            RemoteLaneCommandOutcome(0, f"{SHA}\trefs/heads/dev\n", ""),
        ]
    )
    result = HandlerRemoteLaneRef(git).handle(_request(omni_home="/registry"))
    url = "https://github.com/owner_a/repo_a.git"
    assert git.argvs[0] == ["git", "ls-remote", "--symref", url, "HEAD"]
    assert git.argvs[1][:3] == ["git", "ls-remote", url]
    assert (result.sha, result.landing_branch, result.remote) == (SHA, "dev", url)


def test_the_internal_repo_reads_its_own_home_first() -> None:
    git = FakeGit(
        [HEAD_MAIN, RemoteLaneCommandOutcome(0, f"{SHA}\trefs/heads/main\n", "")],
        clones=frozenset({"/internal", "/registry/omnibase_internal"}),
    )
    HandlerRemoteLaneRef(git).handle(
        _request(
            repo="omnibase_internal",
            ref="main",
            omni_home="/registry",
            omnibase_internal_home="/internal",
        )
    )
    assert git.argvs[0] == [
        "git",
        "-C",
        "/internal",
        "ls-remote",
        "--symref",
        "origin",
        "HEAD",
    ]
    assert git.argvs[1][:5] == ["git", "-C", "/internal", "ls-remote", "origin"]


_READ_FAILED = EnumRemoteLaneRefRefusal.READ_FAILED
_NO_DEFAULT = EnumRemoteLaneRefRefusal.NO_DEFAULT_BRANCH


@pytest.mark.parametrize(
    ("ref", "outcomes", "error", "refusal"),
    [
        (
            None,
            [RemoteLanePortError("FileNotFoundError: git")],
            "ls-remote could not run: FileNotFoundError: git",
            _READ_FAILED,
        ),
        (
            None,
            [RemoteLaneCommandOutcome(128, "", "fatal: no remote\n")],
            "ls-remote exited 128: fatal: no remote",
            _READ_FAILED,
        ),
        (
            None,
            [RemoteLaneCommandOutcome(0, f"{SHA}\tHEAD\n", "")],
            "the remote names no HEAD branch",
            _NO_DEFAULT,
        ),
        (
            None,
            [
                RemoteLaneCommandOutcome(0, "ref: refs/heads/dev\tHEAD\n", ""),
                RemoteLaneCommandOutcome(0, "", ""),
            ],
            "the remote has no branch or tag dev",
            _NO_DEFAULT,
        ),
        (
            "feature",
            [HEAD_MAIN, RemoteLaneCommandOutcome(0, f"{SHA}\trefs/heads/main\n", "")],
            "the remote has no branch or tag feature",
            EnumRemoteLaneRefRefusal.BRANCH_NOT_PUSHED,
        ),
        (
            "feature",
            [
                HEAD_MAIN,
                RemoteLaneCommandOutcome(0, f"{SHA}\trefs/heads/feature\n", ""),
            ],
            "the remote has no default branch main",
            EnumRemoteLaneRefRefusal.DEFAULT_BRANCH_NOT_ON_REMOTE,
        ),
    ],
)
def test_a_failed_read_is_a_typed_result(
    ref: str | None,
    outcomes: list[RemoteLaneCommandOutcome | Exception],
    error: str,
    refusal: EnumRemoteLaneRefRefusal,
) -> None:
    result = HandlerRemoteLaneRef(FakeGit(outcomes)).handle(_request(ref=ref))
    assert (result.sha, result.error, result.refusal) == (None, error, refusal)


def test_a_named_branch_reads_the_default_branch_before_send() -> None:
    git = FakeGit(
        [
            HEAD_MAIN,
            RemoteLaneCommandOutcome(
                0, f"{SHA}\trefs/heads/main\n{'d' * 40}\trefs/heads/feature\n", ""
            ),
        ]
    )
    result = HandlerRemoteLaneRef(git).handle(_request(ref="feature"))
    assert (result.sha, result.landing_branch, result.refusal) == (
        "d" * 40,
        "main",
        None,
    )
    assert (
        git.argvs[0][-2:]
        == ["--symref", "https://github.com/owner_a/repo_a.git", "HEAD"][-2:]
    )
    assert "refs/heads/main" in git.argvs[1]
    assert "refs/heads/feature" in git.argvs[1]


def test_a_branch_outranks_a_tag_of_the_same_name() -> None:
    tag, branch = "b" * 40, "c" * 40
    git = FakeGit(
        [
            HEAD_MAIN,
            RemoteLaneCommandOutcome(
                0,
                f"{tag}\trefs/tags/x\n{branch}\trefs/heads/x\n{SHA}\trefs/heads/main\n",
                "",
            ),
        ]
    )
    assert HandlerRemoteLaneRef(git).handle(_request(ref="x")).sha == branch

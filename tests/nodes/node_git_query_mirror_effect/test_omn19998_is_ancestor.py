# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The is_ancestor operation over the git mirror (OMN-19998).

Whether one sha is an ancestor of another in a named repository's bare mirror:
a pure read over the objects the mirror already holds, unread when the mirror
lacks either sha, and it never fetches. Every test runs against the scratch
upstream repository on disk from ``test_git_query_mirror.py`` -- its helpers
are imported, not redefined -- so nothing here touches the network or the
GitHub API.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import omnimarket.nodes.node_git_query_mirror_effect.__main__ as git_query_mirror_cli
from omnimarket.nodes.node_git_query_mirror_effect.__main__ import main
from omnimarket.nodes.node_git_query_mirror_effect.handlers.handler_git_query_mirror import (
    HandlerGitQueryMirrorEffect,
)
from omnimarket.nodes.node_git_query_mirror_effect.models.model_git_query_mirror import (
    EnumGitQueryOperation,
    EnumGitQuerySource,
    ModelGitQueryRequest,
    ModelGitQueryResponse,
)
from tests.nodes.node_git_query_mirror_effect.test_git_query_mirror import (
    REPO,
    FakeGh,
    Upstream,
    break_remote,
    commit_file,
    git,
    make_mirror,
    req,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return Upstream(tmp_path)


def is_ancestor_req(ancestor: str, descendant: str) -> ModelGitQueryRequest:
    return req(
        EnumGitQueryOperation.IS_ANCESTOR,
        ancestor_sha=ancestor,
        descendant_sha=descendant,
    )


def unfetched_commit(upstream: Upstream) -> str:
    """A commit upstream holds but no ref can reach, so a sync never fetches it."""
    git(upstream.path, "checkout", "-q", "-b", "unfetched")
    sha = commit_file(upstream.path, "z.txt", "z\n", "never fetched")
    git(upstream.path, "checkout", "-q", "dev")
    git(upstream.path, "branch", "-q", "-D", "unfetched")
    return sha


# --- answers from the mirror ------------------------------------------------------


def test_older_commit_is_ancestor_of_newer(tmp_path: Path, upstream: Upstream) -> None:
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    res = m.query(is_ancestor_req(upstream.pr4, upstream.dev))
    assert res.ok, res.error
    assert res.is_ancestor is True
    assert res.source is EnumGitQuerySource.MIRROR
    assert res.ancestor_sha == upstream.pr4
    assert res.descendant_sha == upstream.dev
    assert res.pr_number is None
    assert res.mirror_path is not None
    assert res.watermark_age_s is not None


def test_the_same_shas_reversed_answer_false(
    tmp_path: Path, upstream: Upstream
) -> None:
    """Failing control: a handler that always says True fails here."""
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    res = m.query(is_ancestor_req(upstream.dev, upstream.pr4))
    assert res.ok
    assert res.is_ancestor is False
    assert res.source is EnumGitQuerySource.MIRROR


def test_a_sha_is_an_ancestor_of_itself(tmp_path: Path, upstream: Upstream) -> None:
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    res = m.query(is_ancestor_req(upstream.dev, upstream.dev))
    assert res.ok
    assert res.is_ancestor is True


# --- unread: the mirror lacks a sha, or does not exist -----------------------------


@pytest.mark.parametrize("position", ["ancestor", "descendant"])
def test_sha_absent_from_mirror_leaves_the_answer_unread(
    tmp_path: Path, upstream: Upstream, position: str
) -> None:
    gh = FakeGh({})
    m = make_mirror(tmp_path, upstream, gh)
    m.sync(REPO)
    ghost = unfetched_commit(upstream)
    # A broken remote would fail any fetch this operation attempted.
    break_remote(tmp_path)
    if position == "ancestor":
        ask = is_ancestor_req(ghost, upstream.dev)
    else:
        ask = is_ancestor_req(upstream.dev, ghost)
    res = m.query(ask)
    assert res.ok is False
    assert res.is_ancestor is None
    assert res.error is not None
    assert ghost in res.error
    assert res.ancestor_sha == ask.ancestor_sha
    assert res.descendant_sha == ask.descendant_sha
    assert res.mirror_path is not None
    assert res.watermark_age_s is not None
    assert gh.calls == []


def test_no_mirror_yet_is_unavailable(tmp_path: Path, upstream: Upstream) -> None:
    m = make_mirror(tmp_path, upstream)
    res = m.query(is_ancestor_req(upstream.pr4, upstream.dev))
    assert res.ok is False
    assert res.source is EnumGitQuerySource.UNAVAILABLE
    assert res.is_ancestor is None
    assert res.error == "no mirror yet"
    assert res.watermark_age_s is None


# --- request validation ------------------------------------------------------------


def test_is_ancestor_needs_both_shas() -> None:
    with pytest.raises(ValidationError) as missing:
        ModelGitQueryRequest(
            operation=EnumGitQueryOperation.IS_ANCESTOR,
            repo=REPO,
            descendant_sha="d" * 40,
        )
    assert "is_ancestor" in str(missing.value)


def test_short_sha_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelGitQueryRequest(
            operation=EnumGitQueryOperation.IS_ANCESTOR,
            repo=REPO,
            ancestor_sha="abc123",
            descendant_sha="d" * 40,
        )


def test_pr_number_is_forbidden_on_is_ancestor() -> None:
    with pytest.raises(ValidationError) as scoped:
        ModelGitQueryRequest(
            operation=EnumGitQueryOperation.IS_ANCESTOR,
            repo=REPO,
            pr_number=1,
            ancestor_sha="a" * 40,
            descendant_sha="d" * 40,
        )
    assert "is_ancestor" in str(scoped.value)


def test_shas_are_forbidden_on_other_operations() -> None:
    with pytest.raises(ValidationError) as elsewhere:
        ModelGitQueryRequest(
            operation=EnumGitQueryOperation.HEAD_SHA,
            repo=REPO,
            pr_number=1,
            ancestor_sha="a" * 40,
        )
    assert "head_sha" in str(elsewhere.value)


# --- the chain: handler in, typed response event out --------------------------------


def _handler(tmp_path: Path, upstream: Upstream) -> HandlerGitQueryMirrorEffect:
    return HandlerGitQueryMirrorEffect(
        remote_url_for=lambda _repo: str(upstream.path), gh_runner=FakeGh({})
    )


def test_handler_chain_carries_the_golden_result(
    tmp_path: Path, upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_GIT_QUERY_MIRROR_ROOT", str(tmp_path / "mirrors"))
    handler = _handler(tmp_path, upstream)
    asyncio.run(handler.handle(req(EnumGitQueryOperation.SYNC)))
    ask = is_ancestor_req(upstream.pr4, upstream.dev)
    out = asyncio.run(handler.handle(ask))
    (event,) = out.events
    assert isinstance(event, ModelGitQueryResponse)
    assert event.correlation_id == ask.correlation_id
    (result,) = event.results
    assert result.ok
    assert result.is_ancestor is True
    assert result.source is EnumGitQuerySource.MIRROR
    assert result.ancestor_sha == upstream.pr4
    assert result.descendant_sha == upstream.dev


def test_handler_chain_carries_the_unread_result(
    tmp_path: Path, upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_GIT_QUERY_MIRROR_ROOT", str(tmp_path / "mirrors"))
    handler = _handler(tmp_path, upstream)
    asyncio.run(handler.handle(req(EnumGitQueryOperation.SYNC)))
    ghost = unfetched_commit(upstream)
    ask = is_ancestor_req(ghost, upstream.dev)
    out = asyncio.run(handler.handle(ask))
    (event,) = out.events
    assert isinstance(event, ModelGitQueryResponse)
    (result,) = event.results
    assert result.ok is False
    assert result.is_ancestor is None
    assert result.error is not None
    assert ghost in result.error


# --- the CLI -------------------------------------------------------------------------


def cli_is_ancestor(ancestor: str, descendant: str) -> int:
    return main(
        [
            "is-ancestor",
            "--repo",
            "scratch",
            "--ancestor",
            ancestor,
            "--descendant",
            descendant,
        ]
    )


def test_cli_is_ancestor_answers_true_and_false(
    tmp_path: Path,
    upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ONEX_GIT_QUERY_MIRROR_ROOT", str(tmp_path / "mirrors"))
    make_mirror(tmp_path, upstream).sync(REPO)
    for ancestor, descendant, expected in (
        (upstream.pr4, upstream.dev, True),
        (upstream.dev, upstream.pr4, False),
    ):
        assert cli_is_ancestor(ancestor, descendant) == 0
        payload = json.loads(capsys.readouterr().out.strip())
        assert payload["operation"] == "is_ancestor"
        assert payload["source"] == "mirror"
        assert payload["is_ancestor"] is expected


def test_cli_is_ancestor_unread_exits_1(
    tmp_path: Path,
    upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ONEX_GIT_QUERY_MIRROR_ROOT", str(tmp_path / "mirrors"))
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    ghost = unfetched_commit(upstream)
    assert cli_is_ancestor(ghost, upstream.dev) == 1
    payload = json.loads(capsys.readouterr().out.strip())
    # exclude_none drops the unread answer from the JSON emit
    assert "is_ancestor" not in payload
    assert payload["ok"] is False


def test_cli_is_ancestor_usage_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ONEX_GIT_QUERY_MIRROR_ROOT", str(tmp_path))

    def fail_query(*_args: object, **_kwargs: object) -> None:
        pytest.fail("query must not run after a usage error")

    monkeypatch.setattr(git_query_mirror_cli.GitQueryMirror, "query", fail_query)

    def last_error_line() -> str:
        return capsys.readouterr().err.strip().splitlines()[-1]

    assert main(["is-ancestor", "--repo", "scratch", "--ancestor", "a" * 40]) == 2
    assert "descendant" in last_error_line()
    assert (
        main(
            [
                "is-ancestor",
                "--repo",
                "scratch",
                "--ancestor",
                "a" * 40,
                "--descendant",
                "d" * 40,
                "--pr",
                "1",
            ]
        )
        == 2
    )
    assert "takes no --pr" in last_error_line()
    assert (
        main(["head-sha", "--repo", "scratch", "--pr", "1", "--ancestor", "a" * 40])
        == 2
    )
    assert "takes no --ancestor" in last_error_line()

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for node_git_query_mirror_effect (OMN-19617).

Every test runs against a scratch upstream repository on disk that carries
GitHub-shaped pull refs (refs/pull/<n>/head), so nothing here touches the
network or the GitHub API.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)
from pydantic import ValidationError

import omnimarket.nodes.node_git_query_mirror_effect.__main__ as git_query_mirror_cli
from omnimarket.nodes.node_git_query_mirror_effect.__main__ import main
from omnimarket.nodes.node_git_query_mirror_effect.git_mirror import (
    GitQueryMirror,
    resolve_mirror_root,
)
from omnimarket.nodes.node_git_query_mirror_effect.handlers.handler_git_query_mirror import (
    HandlerGitQueryMirrorEffect,
)
from omnimarket.nodes.node_git_query_mirror_effect.models.model_git_query_mirror import (
    EnumGitQueryOperation,
    EnumGitQuerySource,
    EnumOnBaseReason,
    ModelGitQueryRequest,
    ModelGitQueryResponse,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/scratch"


def git(cwd: Path, *args: str) -> str:
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    return subprocess.run(
        ["git", *identity, *args],
        cwd=cwd,
        env=scrub_git_location_env(os.environ),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_file(repo: Path, path: str, text: str, msg: str) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


class Upstream:
    """A scratch 'GitHub': branch dev plus refs/pull/<n>/head refs."""

    def __init__(self, root: Path) -> None:
        self.path = root / "upstream"
        self.path.mkdir()
        git(self.path, "init", "-q", "-b", "dev")
        commit_file(self.path, "a.txt", "one\ntwo\nthree\n", "base")
        commit_file(self.path, "b.txt", "bee\n", "base b")
        base = git(self.path, "rev-parse", "HEAD")
        # PR 1: clean, adds a file and edits b.txt
        git(self.path, "checkout", "-q", "-b", "pr1", base)
        commit_file(self.path, "src/new.py", "print('hi')\n", "pr1: add new.py")
        commit_file(self.path, "b.txt", "bee\nbuzz\n", "pr1: extend b")
        self.pr1 = git(self.path, "rev-parse", "HEAD")
        # PR 2: conflicts with a later dev edit to the same line of a.txt
        git(self.path, "checkout", "-q", "-b", "pr2", base)
        self.pr2 = commit_file(self.path, "a.txt", "one\nTWO-pr\nthree\n", "pr2")
        # PR 3: superseded -- the same change lands on dev by a separate commit
        git(self.path, "checkout", "-q", "-b", "pr3", base)
        self.pr3 = commit_file(self.path, "c.txt", "sea\n", "pr3: add c")
        # PR 4: merged -- its head is an ancestor of dev
        git(self.path, "checkout", "-q", "-b", "pr4", base)
        self.pr4 = commit_file(self.path, "d.txt", "dee\n", "pr4: add d")
        git(self.path, "checkout", "-q", "dev")
        git(self.path, "merge", "-q", "--no-ff", "-m", "merge pr4", "pr4")
        commit_file(self.path, "a.txt", "one\nTWO-dev\nthree\n", "dev edits a")
        commit_file(self.path, "c.txt", "sea\n", "dev carries pr3's change")
        for n, sha in ((1, self.pr1), (2, self.pr2), (3, self.pr3), (4, self.pr4)):
            git(self.path, "update-ref", f"refs/pull/{n}/head", sha)
        self.dev = git(self.path, "rev-parse", "dev")

    def push_pr(self, n: int, path: str, text: str) -> str:
        git(self.path, "checkout", "-q", f"pr{n}")
        sha = commit_file(self.path, path, text, f"pr{n}: more")
        git(self.path, "update-ref", f"refs/pull/{n}/head", sha)
        git(self.path, "checkout", "-q", "dev")
        return sha


class FakeGh:
    """Records gh argv and answers from a table; never runs gh."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        for needle, out in self.answers.items():
            if needle in " ".join(argv):
                return 0, out, ""
        return 1, "", "no answer"


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return Upstream(tmp_path)


def make_mirror(
    tmp_path: Path, upstream: Upstream, gh: FakeGh | None = None
) -> GitQueryMirror:
    return GitQueryMirror(
        root=tmp_path / "mirrors",
        remote_url_for=lambda _repo: str(upstream.path),
        gh_runner=gh or FakeGh({}),
    )


def req(
    op: EnumGitQueryOperation, pr: int | None = None, **kw: object
) -> ModelGitQueryRequest:
    return ModelGitQueryRequest(operation=op, repo=REPO, pr_number=pr, **kw)  # type: ignore[arg-type]


# --- AC1: sync ----------------------------------------------------------------


def test_sync_builds_mirror_with_pull_refs_and_watermark(
    tmp_path: Path, upstream: Upstream
) -> None:
    m = make_mirror(tmp_path, upstream)
    res = m.sync(REPO)
    assert res.ok, res.error
    assert res.pull_refs == 4
    assert res.head_refs is not None
    assert res.head_refs >= 1
    mirror = Path(res.mirror_path or "")
    assert git(mirror, "rev-parse", "refs/pull/1/head") == upstream.pr1
    assert git(mirror, "rev-parse", "refs/heads/dev") == upstream.dev
    # pull refs live in their own namespace, never under refs/remotes
    assert git(mirror, "for-each-ref", "refs/remotes") == ""
    wm = json.loads((mirror / "onex_query_watermark.json").read_text())
    assert wm["repo"] == REPO
    assert wm["ok"] is True


def test_sync_never_writes_a_ref_into_a_canonical_clone(
    tmp_path: Path, upstream: Upstream
) -> None:
    canonical = tmp_path / "omni_home" / "scratch"
    git(tmp_path, "clone", "-q", str(upstream.path), str(canonical))
    before = git(canonical, "for-each-ref")
    m = GitQueryMirror(
        root=tmp_path / "mirrors",
        remote_url_for=lambda _repo: str(upstream.path),
        gh_runner=FakeGh({}),
        reference_clone_for=lambda _repo: canonical,
    )
    assert m.sync(REPO).ok
    assert m.query(req(EnumGitQueryOperation.HEAD_SHA, 1, max_age_s=0)).ok
    assert git(canonical, "for-each-ref") == before
    # the mirror stands alone: no alternates pointing back at the canonical clone
    mirror = tmp_path / "mirrors" / "OmniNode-ai" / "scratch.git"
    assert not (mirror / "objects" / "info" / "alternates").exists()


def test_sync_prunes_inside_its_own_namespace_only(
    tmp_path: Path, upstream: Upstream
) -> None:
    m = make_mirror(tmp_path, upstream)
    assert m.sync(REPO).ok
    git(upstream.path, "branch", "-q", "-D", "pr3")
    git(upstream.path, "update-ref", "-d", "refs/pull/3/head")
    assert m.sync(REPO).ok
    mirror = tmp_path / "mirrors" / "OmniNode-ai" / "scratch.git"
    refs = git(mirror, "for-each-ref", "--format=%(refname)")
    assert "refs/pull/3/head" not in refs
    assert "refs/heads/pr3" not in refs
    assert "refs/pull/1/head" in refs


# --- AC2: queries match git ------------------------------------------------------


def test_head_sha_matches_upstream_pull_ref(tmp_path: Path, upstream: Upstream) -> None:
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    res = m.query(req(EnumGitQueryOperation.HEAD_SHA, 1))
    assert res.ok
    assert res.source is EnumGitQuerySource.MIRROR
    assert res.head_sha == upstream.pr1
    assert res.base_ref == "dev"
    assert res.base_sha == upstream.dev
    assert res.watermark_age_s is not None
    assert res.watermark_age_s < 60


def test_changed_files_match_three_dot_diff(tmp_path: Path, upstream: Upstream) -> None:
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    res = m.query(req(EnumGitQueryOperation.CHANGED_FILES, 1))
    expected = git(
        upstream.path, "diff", "--name-only", "--no-renames", f"dev...{upstream.pr1}"
    )
    assert res.ok
    assert res.files == expected.splitlines() == ["b.txt", "src/new.py"]


def test_diff_is_the_three_dot_diff(tmp_path: Path, upstream: Upstream) -> None:
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    res = m.query(req(EnumGitQueryOperation.DIFF, 1))
    assert res.ok
    assert res.diff is not None
    assert (
        res.diff.strip() == git(upstream.path, "diff", f"dev...{upstream.pr1}").strip()
    )
    assert "+buzz" in res.diff


def test_conflicts_clean_and_conflicting(tmp_path: Path, upstream: Upstream) -> None:
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    clean = m.query(req(EnumGitQueryOperation.CONFLICTS, 1))
    assert clean.ok
    assert clean.conflicts is False
    assert clean.conflicted_files == []
    bad = m.query(req(EnumGitQueryOperation.CONFLICTS, 2))
    assert bad.ok
    assert bad.conflicts is True
    assert bad.conflicted_files == ["a.txt"]


def test_on_base_detects_superseded_merged_and_live(
    tmp_path: Path, upstream: Upstream
) -> None:
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    sup = m.query(req(EnumGitQueryOperation.ON_BASE, 3))
    assert sup.ok
    assert sup.on_base is True
    assert sup.on_base_reason is EnumOnBaseReason.MERGE_ADDS_NOTHING
    merged = m.query(req(EnumGitQueryOperation.ON_BASE, 4))
    assert merged.on_base is True
    assert merged.on_base_reason is EnumOnBaseReason.HEAD_IS_ANCESTOR
    live = m.query(req(EnumGitQueryOperation.ON_BASE, 1))
    assert live.on_base is False
    assert live.on_base_reason is EnumOnBaseReason.MERGE_ADDS_CHANGES
    conflicted = m.query(req(EnumGitQueryOperation.ON_BASE, 2))
    assert conflicted.on_base is False
    assert conflicted.on_base_reason is EnumOnBaseReason.CONFLICTS


def test_log_lists_pr_commits_oldest_first(tmp_path: Path, upstream: Upstream) -> None:
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    res = m.query(req(EnumGitQueryOperation.LOG, 1))
    assert res.ok
    assert res.commits is not None
    assert [c.subject for c in res.commits] == ["pr1: add new.py", "pr1: extend b"]
    assert res.commits[-1].sha == upstream.pr1


def test_explicit_base_branch(tmp_path: Path, upstream: Upstream) -> None:
    git(upstream.path, "branch", "main", upstream.pr4)
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    res = m.query(req(EnumGitQueryOperation.CHANGED_FILES, 1, base="main"))
    assert res.ok
    assert res.base_ref == "main"
    assert res.files == ["b.txt", "src/new.py"]


# --- AC3: watermark, targeted refresh, gh fallback ----------------------------------


def age_watermark(tmp_path: Path, seconds: int) -> None:
    wm_path = (
        tmp_path
        / "mirrors"
        / "OmniNode-ai"
        / "scratch.git"
        / "onex_query_watermark.json"
    )
    wm = json.loads(wm_path.read_text())
    wm["fetched_at_epoch"] = wm["fetched_at_epoch"] - seconds
    wm_path.write_text(json.dumps(wm))


def test_stale_mirror_refreshes_the_pr_ref_by_git_first(
    tmp_path: Path, upstream: Upstream
) -> None:
    gh = FakeGh({})
    m = make_mirror(tmp_path, upstream, gh)
    m.sync(REPO)
    new_head = upstream.push_pr(1, "b.txt", "bee\nbuzz\nbuzz2\n")
    fresh = m.query(req(EnumGitQueryOperation.HEAD_SHA, 1))
    assert fresh.source is EnumGitQuerySource.MIRROR
    assert fresh.head_sha == upstream.pr1
    age_watermark(tmp_path, 3600)
    res = m.query(req(EnumGitQueryOperation.HEAD_SHA, 1))
    assert res.ok
    assert res.source is EnumGitQuerySource.MIRROR_REFRESHED
    assert res.head_sha == new_head
    assert gh.calls == []


def test_max_age_zero_always_refreshes(tmp_path: Path, upstream: Upstream) -> None:
    m = make_mirror(tmp_path, upstream)
    m.sync(REPO)
    new_head = upstream.push_pr(1, "e.txt", "e\n")
    res = m.query(req(EnumGitQueryOperation.HEAD_SHA, 1, max_age_s=0))
    assert res.source is EnumGitQuerySource.MIRROR_REFRESHED
    assert res.head_sha == new_head


def test_missing_mirror_is_built_on_first_query(
    tmp_path: Path, upstream: Upstream
) -> None:
    m = make_mirror(tmp_path, upstream)
    res = m.query(req(EnumGitQueryOperation.HEAD_SHA, 2))
    assert res.ok
    assert res.source is EnumGitQuerySource.MIRROR_REFRESHED
    assert res.head_sha == upstream.pr2


def break_remote(tmp_path: Path) -> None:
    mirror = tmp_path / "mirrors" / "OmniNode-ai" / "scratch.git"
    git(mirror, "remote", "set-url", "origin", str(tmp_path / "no-such-remote"))


def test_failed_refresh_falls_back_to_gh_and_says_so(
    tmp_path: Path, upstream: Upstream
) -> None:
    gh = FakeGh({f"repos/{REPO}/pulls/1": "f" * 40})
    m = make_mirror(tmp_path, upstream, gh)
    m.sync(REPO)
    break_remote(tmp_path)
    age_watermark(tmp_path, 3600)
    res = m.query(req(EnumGitQueryOperation.HEAD_SHA, 1))
    assert res.ok
    assert res.source is EnumGitQuerySource.GH_FALLBACK
    assert res.head_sha == "f" * 40
    assert len(gh.calls) == 1
    assert gh.calls[0][:2] == ["gh", "api"]


def test_changed_files_gh_fallback(tmp_path: Path, upstream: Upstream) -> None:
    gh = FakeGh({f"repos/{REPO}/pulls/1/files": "x.py\ny.py"})
    m = make_mirror(tmp_path, upstream, gh)
    m.sync(REPO)
    break_remote(tmp_path)
    res = m.query(req(EnumGitQueryOperation.CHANGED_FILES, 1, max_age_s=0))
    assert res.source is EnumGitQuerySource.GH_FALLBACK
    assert res.files == [
        "x.py",
        "y.py",
    ]


def test_fallback_disabled_reports_unavailable(
    tmp_path: Path, upstream: Upstream
) -> None:
    gh = FakeGh({f"repos/{REPO}/pulls/1": "f" * 40})
    m = make_mirror(tmp_path, upstream, gh)
    m.sync(REPO)
    break_remote(tmp_path)
    res = m.query(
        req(EnumGitQueryOperation.HEAD_SHA, 1, max_age_s=0, allow_gh_fallback=False)
    )
    assert res.ok is False
    assert res.source is EnumGitQuerySource.UNAVAILABLE
    assert res.error
    assert gh.calls == []


def test_conflicts_have_no_gh_fallback(tmp_path: Path, upstream: Upstream) -> None:
    gh = FakeGh({"": "anything"})
    m = make_mirror(tmp_path, upstream, gh)
    m.sync(REPO)
    break_remote(tmp_path)
    res = m.query(req(EnumGitQueryOperation.CONFLICTS, 7, max_age_s=0))
    assert res.ok is False
    assert res.source is EnumGitQuerySource.UNAVAILABLE
    assert gh.calls == []


def test_status_reports_watermark_without_fetching(
    tmp_path: Path, upstream: Upstream
) -> None:
    m = make_mirror(tmp_path, upstream)
    missing = m.query(req(EnumGitQueryOperation.STATUS))
    assert missing.ok is False
    assert missing.watermark_age_s is None
    m.sync(REPO)
    res = m.query(req(EnumGitQueryOperation.STATUS))
    assert res.ok
    assert res.pull_refs == 4
    assert res.watermark_age_s is not None


# --- request validation, root resolution, handler ---------------------------------


def test_main_returns_usage_error_when_mirror_root_is_unset_and_error_returns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_GIT_QUERY_MIRROR_ROOT", raising=False)
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    messages: list[str] = []

    def record_error(_parser: argparse.ArgumentParser, message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(argparse.ArgumentParser, "error", record_error)

    assert main(["status"]) == 2
    stderr = capsys.readouterr().err
    assert "ONEX_GIT_QUERY_MIRROR_ROOT" in stderr
    assert "ONEX_STATE_DIR" in stderr


def test_main_returns_usage_error_when_pr_is_missing_and_error_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ONEX_GIT_QUERY_MIRROR_ROOT", str(tmp_path))
    messages: list[str] = []

    def record_error(_parser: argparse.ArgumentParser, message: str) -> None:
        messages.append(message)

    def fail_query(*_args: object, **_kwargs: object) -> None:
        pytest.fail("query must not run after a usage error")

    monkeypatch.setattr(argparse.ArgumentParser, "error", record_error)
    monkeypatch.setattr(git_query_mirror_cli.GitQueryMirror, "query", fail_query)

    assert main(["head-sha", "--repo", "omnimarket"]) == 2


def test_main_returns_usage_error_when_mirror_root_is_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_GIT_QUERY_MIRROR_ROOT", raising=False)
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)

    assert main(["status"]) == 2
    stderr = capsys.readouterr().err
    assert "ONEX_GIT_QUERY_MIRROR_ROOT" in stderr
    assert "ONEX_STATE_DIR" in stderr


def test_pr_scoped_operation_requires_pr_number() -> None:
    with pytest.raises(ValidationError):
        ModelGitQueryRequest(operation=EnumGitQueryOperation.DIFF, repo=REPO)


def test_mirror_root_fails_fast_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_GIT_QUERY_MIRROR_ROOT", raising=False)
    monkeypatch.delenv("ONEX_STATE_DIR", raising=False)
    with pytest.raises(KeyError):
        resolve_mirror_root()
    monkeypatch.setenv("ONEX_STATE_DIR", "/x/state")
    assert resolve_mirror_root() == Path("/x/state/git-query-mirrors")
    monkeypatch.setenv("ONEX_GIT_QUERY_MIRROR_ROOT", "/y/m")
    assert resolve_mirror_root() == Path("/y/m")


def test_handler_emits_typed_response(
    tmp_path: Path, upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_GIT_QUERY_MIRROR_ROOT", str(tmp_path / "mirrors"))
    handler = HandlerGitQueryMirrorEffect(
        remote_url_for=lambda _repo: str(upstream.path), gh_runner=FakeGh({})
    )
    out = asyncio.run(handler.handle(req(EnumGitQueryOperation.HEAD_SHA, 1)))
    (event,) = out.events
    assert isinstance(event, ModelGitQueryResponse)
    (result,) = event.results
    assert result.head_sha == upstream.pr1

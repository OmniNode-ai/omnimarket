# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Merge detection from the git mirror (OMN-19834).

A sync of the mirror finds the squash merges that landed on the default branch
since the previous sync and the handler publishes one
``onex.evt.github.pr-merged.v1`` event per merge with source ``mirror``. Every
test runs against a scratch upstream on disk: no network, no GitHub API.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.events.github import pr_merged_event_id
from omnimarket.events.topics import PR_MERGED_TOPIC_V1
from omnimarket.nodes.node_git_query_mirror_effect.git_mirror import GitQueryMirror
from omnimarket.nodes.node_git_query_mirror_effect.handlers.handler_git_query_mirror import (
    HandlerGitQueryMirrorEffect,
)
from omnimarket.nodes.node_git_query_mirror_effect.models.model_git_query_mirror import (
    EnumGitQueryOperation,
    ModelGitQueryRequest,
    ModelGitQueryResponse,
    ModelMirrorPrMerged,
)

pytestmark = pytest.mark.unit

REPO = "OmniNode-ai/scratch"
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_git_query_mirror_effect/contract.yaml"
)


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


class Upstream:
    """A scratch 'GitHub': branch dev, PR branches, refs/pull/<n>/head, squash merges."""

    def __init__(self, root: Path) -> None:
        self.path = root / "upstream"
        self.path.mkdir()
        git(self.path, "init", "-q", "-b", "dev")
        self.commit("a.txt", "one\n", "base")

    def commit(self, path: str, text: str, msg: str) -> str:
        (self.path / path).parent.mkdir(parents=True, exist_ok=True)
        (self.path / path).write_text(text)
        git(self.path, "add", path)
        git(self.path, "commit", "-q", "-m", msg)
        return git(self.path, "rev-parse", "HEAD")

    def open_pr(self, n: int, branch: str, path: str) -> str:
        git(self.path, "checkout", "-q", "-b", branch, "dev")
        sha = self.commit(path, f"pr {n}\n", f"work for {n}")
        git(self.path, "update-ref", f"refs/pull/{n}/head", sha)
        git(self.path, "checkout", "-q", "dev")
        return sha

    def squash_merge(self, branch: str, title: str, delete_branch: bool = True) -> str:
        git(self.path, "checkout", "-q", "dev")
        git(self.path, "merge", "--squash", branch)
        git(self.path, "commit", "-q", "-m", title)
        if delete_branch:
            git(self.path, "branch", "-q", "-D", branch)
        return git(self.path, "rev-parse", "HEAD")


class TripwireGh:
    """A gh runner that fails the test when anything calls it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(argv)
        raise AssertionError(f"gh was called: {argv}")


@pytest.fixture
def upstream(tmp_path: Path) -> Upstream:
    return Upstream(tmp_path)


def make_mirror(
    tmp_path: Path,
    upstream: Upstream,
    seen: Callable[[str, int], bool] | None = None,
    gh: TripwireGh | None = None,
) -> GitQueryMirror:
    return GitQueryMirror(
        root=tmp_path / "mirrors",
        remote_url_for=lambda _repo: str(upstream.path),
        gh_runner=gh or TripwireGh(),
        published_merges=seen,
    )


# --- AC1: one event per new squash merge, none on the second sync -------------


def test_squash_merge_yields_one_mirror_event_and_second_sync_none(
    tmp_path: Path, upstream: Upstream
) -> None:
    pr_head = upstream.open_pr(7, "omn-123-feature", "f.txt")
    m = make_mirror(tmp_path, upstream)
    baseline = m.sync(REPO)
    assert baseline.ok, baseline.error
    assert baseline.merged_prs == []

    merge_sha = upstream.squash_merge("omn-123-feature", "add f (#7)")
    first = m.sync(REPO)
    assert first.ok, first.error
    assert first.merged_prs is not None
    (event,) = first.merged_prs
    assert event.source == "mirror"
    assert event.topic == PR_MERGED_TOPIC_V1
    assert event.repo == REPO
    assert event.pr_number == 7
    assert event.merge_sha == merge_sha
    assert event.base_ref == "dev"
    assert event.branch == "omn-123-feature"
    assert event.branch_resolved is True
    assert event.ticket == "OMN-123"
    assert event.event_id == pr_merged_event_id(REPO, 7, merge_sha)
    assert event.head_sha == pr_head

    second = m.sync(REPO)
    assert second.ok, second.error
    assert second.merged_prs == []


def test_first_sync_is_a_baseline_and_publishes_history_of_nothing(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.open_pr(1, "omn-1-old", "old.txt")
    upstream.squash_merge("omn-1-old", "old work (#1)")
    result = make_mirror(tmp_path, upstream).sync(REPO)
    assert result.ok, result.error
    assert result.merged_prs == []


def test_two_merges_between_syncs_come_out_oldest_first(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.open_pr(10, "omn-10-a", "a10.txt")
    upstream.open_pr(11, "omn-11-b", "b11.txt")
    m = make_mirror(tmp_path, upstream)
    assert m.sync(REPO).ok
    upstream.squash_merge("omn-10-a", "ten (#10)")
    upstream.squash_merge("omn-11-b", "eleven (#11)")
    result = m.sync(REPO)
    assert result.merged_prs is not None
    assert [e.pr_number for e in result.merged_prs] == [10, 11]


def test_direct_push_without_a_pr_number_is_not_a_merge(
    tmp_path: Path, upstream: Upstream
) -> None:
    m = make_mirror(tmp_path, upstream)
    assert m.sync(REPO).ok
    upstream.commit("x.txt", "x\n", "chore: tidy")
    result = m.sync(REPO)
    assert result.ok, result.error
    assert result.merged_prs == []


def test_branch_deleted_before_the_first_sync_that_saw_it_is_unresolved(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.open_pr(5, "omn-5-quick", "q.txt")
    m = make_mirror(tmp_path, upstream)
    assert m.sync(REPO).ok
    upstream.squash_merge("omn-5-quick", "quick (#5)")
    # PR 6 opens and merges between two syncs: the mirror never saw its branch
    upstream.open_pr(6, "omn-6-unseen", "u.txt")
    upstream.squash_merge("omn-6-unseen", "unseen (#6)")
    result = m.sync(REPO)
    assert result.merged_prs is not None
    by_pr = {e.pr_number: e for e in result.merged_prs}
    assert by_pr[5].branch == "omn-5-quick"
    assert by_pr[5].branch_resolved is True
    assert by_pr[6].branch == "refs/pull/6/head"
    assert by_pr[6].branch_resolved is False


def test_force_push_of_the_base_branch_resets_the_baseline(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.open_pr(2, "omn-2-x", "x2.txt")
    m = make_mirror(tmp_path, upstream)
    assert m.sync(REPO).ok
    upstream.squash_merge("omn-2-x", "x (#2)")
    assert m.sync(REPO).ok
    git(upstream.path, "reset", "-q", "--hard", "HEAD~1")
    upstream.commit("y.txt", "y\n", "rewritten (#3)")
    result = m.sync(REPO)
    assert result.ok, result.error
    assert result.merged_prs == []


# --- AC2: no GitHub API call --------------------------------------------------


def test_detection_makes_no_github_call_with_gh_absent_and_no_token(
    tmp_path: Path, upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_bin = shutil.which("git")
    assert git_bin is not None
    bin_dir = tmp_path / "only-git"
    bin_dir.mkdir()
    (bin_dir / "git").symlink_to(git_bin)
    monkeypatch.setenv("PATH", str(bin_dir))
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    assert shutil.which("gh") is None

    upstream.open_pr(9, "omn-9-nogh", "n.txt")
    gh = TripwireGh()
    m = make_mirror(tmp_path, upstream, gh=gh)
    assert m.sync(REPO).ok
    upstream.squash_merge("omn-9-nogh", "no gh (#9)")
    result = m.sync(REPO)
    assert result.ok, result.error
    assert result.merged_prs is not None
    assert [e.pr_number for e in result.merged_prs] == [9]
    assert gh.calls == []


# --- AC3: dedupe against the Actions producers --------------------------------


def test_pr_already_published_by_actions_is_not_published_again(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.open_pr(20, "omn-20-actions", "a20.txt")
    upstream.open_pr(21, "omn-21-mirror", "a21.txt")
    published_by_actions = {(REPO, 20)}
    m = make_mirror(
        tmp_path, upstream, seen=lambda r, n: (r, n) in published_by_actions
    )
    assert m.sync(REPO).ok
    upstream.squash_merge("omn-20-actions", "actions saw it (#20)")
    upstream.squash_merge("omn-21-mirror", "only the mirror saw it (#21)")
    result = m.sync(REPO)
    assert result.merged_prs is not None
    assert [e.pr_number for e in result.merged_prs] == [21]


def test_same_pr_number_in_another_repository_is_not_deduped(
    tmp_path: Path, upstream: Upstream
) -> None:
    upstream.open_pr(30, "omn-30-other", "o30.txt")
    m = make_mirror(
        tmp_path, upstream, seen=lambda r, n: (r, n) == ("OmniNode-ai/elsewhere", 30)
    )
    assert m.sync(REPO).ok
    upstream.squash_merge("omn-30-other", "other repo has #30 too (#30)")
    result = m.sync(REPO)
    assert result.merged_prs is not None
    assert [e.pr_number for e in result.merged_prs] == [30]


def test_default_ledger_remembers_what_the_mirror_published(
    tmp_path: Path, upstream: Upstream
) -> None:
    """With no injected lookup the mirror's own ledger blocks a re-detection.

    Rewinding the scan watermark re-exposes the same merge to the scan; the
    ledger of what the mirror already published must keep it from going out twice.
    """
    upstream.open_pr(40, "omn-40-ledger", "l40.txt")
    m = GitQueryMirror(
        root=tmp_path / "mirrors",
        remote_url_for=lambda _repo: str(upstream.path),
        gh_runner=TripwireGh(),
    )
    assert m.sync(REPO).ok
    before_merge = git(upstream.path, "rev-parse", "dev")
    upstream.squash_merge("omn-40-ledger", "ledger (#40)")
    first = m.sync(REPO)
    assert first.merged_prs is not None
    assert [e.pr_number for e in first.merged_prs] == [40]
    assert m.is_merge_published(REPO, 40)

    watermark = m.mirror_path(REPO) / "onex_query_watermark.json"
    data = json.loads(watermark.read_text())
    data["merge_scan_sha"] = before_merge
    watermark.write_text(json.dumps(data))
    assert m.sync(REPO).merged_prs == []


# --- handler and contract wiring ----------------------------------------------


def test_handler_emits_a_pr_merged_event_per_merge_after_the_response(
    tmp_path: Path, upstream: Upstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_GIT_QUERY_MIRROR_ROOT", str(tmp_path / "mirrors"))
    upstream.open_pr(50, "omn-50-handler", "h50.txt")
    handler = HandlerGitQueryMirrorEffect(
        remote_url_for=lambda _repo: str(upstream.path), gh_runner=TripwireGh()
    )

    def sync() -> tuple[object, ...]:
        request = ModelGitQueryRequest(operation=EnumGitQueryOperation.SYNC, repo=REPO)
        return asyncio.run(handler.handle(request)).events

    sync()
    upstream.squash_merge("omn-50-handler", "handler (#50)")
    response, merged = sync()
    assert isinstance(response, ModelGitQueryResponse)
    assert isinstance(merged, ModelMirrorPrMerged)
    assert merged.pr_number == 50
    assert merged.source == "mirror"
    assert len(sync()) == 1  # only the response; nothing new merged


def test_contract_declares_the_pr_merged_topic_it_publishes() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert contract["event_bus"]["publish_topics"] == ["onex.evt.github.pr-merged.v1"]
    assert PR_MERGED_TOPIC_V1 == "onex.evt.github.pr-merged.v1"
    published = {e["topic"] for e in contract["published_events"]}
    assert PR_MERGED_TOPIC_V1 in published


def test_event_payload_reads_as_the_projection_model() -> None:
    from omnimarket.events.github import ModelPrMergedEvent

    event = ModelMirrorPrMerged(
        event_id=pr_merged_event_id(REPO, 3, "abcdef1234"),
        repo=REPO,
        branch="omn-3-x",
        base_ref="dev",
        pr_number=3,
        ticket="OMN-3",
        merge_sha="abcdef1234",
        merged_at="2026-10-15T00:00:00+00:00",
        published_at="2026-10-15T00:00:01+00:00",
    )
    projected = ModelPrMergedEvent.model_validate(event.model_dump(mode="json"))
    assert projected.pr_number == 3
    assert projected.event_id == str(event.event_id)

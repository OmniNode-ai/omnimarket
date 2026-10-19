# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The review replay set freezes merged diffs stratified by repo and size under a manifest hash (OMN-20910)."""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_review_replay_set_effect.handlers.handler_review_replay_set import (
    HandlerReviewReplaySet,
    size_bin,
)
from omnimarket.nodes.node_review_replay_set_effect.models import (
    ModelMergedCandidate,
    ModelPinnedItem,
    ModelReplayRepo,
    ModelReviewReplaySetRequest,
)

pytestmark = pytest.mark.unit

BINS = (199, 1000)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
        env=scrub_git_location_env(os.environ),
    ).stdout


class _FakeSource:
    """Candidates per repo; the diff of a candidate is a function of its sha."""

    def __init__(self, candidates: dict[str, list[ModelMergedCandidate]]) -> None:
        self._candidates = candidates

    def list_merged(
        self, repo: ModelReplayRepo, limit: int
    ) -> list[ModelMergedCandidate]:
        return self._candidates[repo.repo][:limit]

    def read_diff(self, repo: ModelReplayRepo, candidate: ModelMergedCandidate) -> str:
        return f"diff --git a/{repo.repo} b/{repo.repo}\n+{candidate.merge_sha}\n"


def _candidate(n: int, lines: int) -> ModelMergedCandidate:
    return ModelMergedCandidate(
        pr_number=n,
        merge_sha=f"{n:040x}",
        parent_sha=f"{n + 100000:040x}",
        title=f"change {n} (#{n})",
        changed_lines=lines,
    )


def _pool() -> dict[str, list[ModelMergedCandidate]]:
    pool: dict[str, list[ModelMergedCandidate]] = {}
    number = 1
    for repo, sizes in (
        ("alpha", (5, 20, 120, 300, 1200, 2000)),
        ("beta", (8, 30, 60, 250, 1100, 1500)),
    ):
        pool[repo] = []
        for _ in range(10):
            for lines in sizes:
                pool[repo].append(_candidate(number, lines))
                number += 1
    return pool


def _request(tmp_path: Path, **overrides: object) -> ModelReviewReplaySetRequest:
    fields: dict[str, Any] = {
        "correlation_id": "corr-1",
        "repos": (
            ModelReplayRepo(repo="alpha", clone_path="/unused/alpha"),
            ModelReplayRepo(repo="beta", clone_path="/unused/beta"),
        ),
        "store_root": str(tmp_path / "store"),
        "target_count": 40,
        "seed": "seed-1",
        "history_limit": 100,
        "size_bin_upper_lines": BINS,
    }
    fields.update(overrides)
    return ModelReviewReplaySetRequest(**fields)


def test_size_bin_boundaries() -> None:
    assert size_bin(1, BINS) == "small"
    assert size_bin(199, BINS) == "small"
    assert size_bin(200, BINS) == "medium"
    assert size_bin(1000, BINS) == "medium"
    assert size_bin(1001, BINS) == "large"


def test_freezes_forty_items_stratified_by_repo_and_size(tmp_path: Path) -> None:
    result = HandlerReviewReplaySet(source=_FakeSource(_pool())).handle(
        _request(tmp_path)
    )
    assert result.item_count == 40
    assert result.short_by == 0
    counts = Counter(result.strata)
    assert len(result.strata) == 6  # 2 repos x 3 size bins
    assert max(result.strata.values()) - min(result.strata.values()) <= 1
    assert sum(result.strata.values()) == 40
    assert counts  # keys are repo/bin strings
    for key in result.strata:
        repo, _, bin_name = key.partition("/")
        assert repo in {"alpha", "beta"}
        assert bin_name in {"small", "medium", "large"}


def test_manifest_hash_is_stable_and_names_the_store_directory(tmp_path: Path) -> None:
    first = HandlerReviewReplaySet(source=_FakeSource(_pool())).handle(
        _request(tmp_path)
    )
    second = HandlerReviewReplaySet(source=_FakeSource(_pool())).handle(
        _request(tmp_path)
    )
    assert first.manifest_hash == second.manifest_hash
    assert len(first.manifest_hash) == 64
    assert second.already_frozen is True
    assert first.already_frozen is False
    assert Path(first.manifest_path).parent.name == first.manifest_hash
    assert len(list((Path(first.manifest_path).parent / "diffs").glob("*.diff"))) == 40


def test_pinned_items_are_in_the_set_whatever_the_seed(tmp_path: Path) -> None:
    pins = (
        ModelPinnedItem(repo="alpha", pr_number=2),
        ModelPinnedItem(repo="beta", pr_number=61),
    )
    for seed in ("seed-1", "seed-2"):
        result = HandlerReviewReplaySet(source=_FakeSource(_pool())).handle(
            _request(tmp_path, seed=seed, pinned=pins)
        )
        assert result.item_count == 40
        manifest = Path(result.manifest_path).read_text(encoding="utf-8")
        assert '"item_id":"alpha-2-' in manifest.replace(" ", "")
        assert '"item_id":"beta-61-' in manifest.replace(" ", "")


def test_a_pin_that_is_not_a_merged_candidate_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pinned item"):
        HandlerReviewReplaySet(source=_FakeSource(_pool())).handle(
            _request(tmp_path, pinned=(ModelPinnedItem(repo="alpha", pr_number=9999),))
        )


def test_seed_changes_the_selection_and_the_hash(tmp_path: Path) -> None:
    one = HandlerReviewReplaySet(source=_FakeSource(_pool())).handle(_request(tmp_path))
    two = HandlerReviewReplaySet(source=_FakeSource(_pool())).handle(
        _request(tmp_path, seed="seed-2")
    )
    assert one.manifest_hash != two.manifest_hash


def test_a_changed_diff_changes_the_hash(tmp_path: Path) -> None:
    class _Edited(_FakeSource):
        def read_diff(
            self, repo: ModelReplayRepo, candidate: ModelMergedCandidate
        ) -> str:
            return super().read_diff(repo, candidate) + "+edited\n"

    one = HandlerReviewReplaySet(source=_FakeSource(_pool())).handle(_request(tmp_path))
    two = HandlerReviewReplaySet(source=_Edited(_pool())).handle(_request(tmp_path))
    assert one.manifest_hash != two.manifest_hash


def test_a_thin_history_reports_short_by_and_still_freezes(tmp_path: Path) -> None:
    pool = {"alpha": _pool()["alpha"][:3], "beta": _pool()["beta"][:2]}
    result = HandlerReviewReplaySet(source=_FakeSource(pool)).handle(_request(tmp_path))
    assert result.item_count == 5
    assert result.short_by == 35


def test_a_tampered_frozen_manifest_is_refused(tmp_path: Path) -> None:
    first = HandlerReviewReplaySet(source=_FakeSource(_pool())).handle(
        _request(tmp_path)
    )
    manifest = Path(first.manifest_path)
    manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen manifest"):
        HandlerReviewReplaySet(source=_FakeSource(_pool())).handle(_request(tmp_path))


def test_git_source_reads_squash_merges_and_their_diffs(tmp_path: Path) -> None:
    repo = tmp_path / "clone"
    repo.mkdir()

    def git(*args: str) -> str:
        return _git(repo, *args)

    git("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-q", "-m", "init")
    (repo / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    git("commit", "-q", "-am", "feat: add lines (#7)")
    (repo / "b.txt").write_text("x\n", encoding="utf-8")
    git("add", "b.txt")
    git("commit", "-q", "-m", "not a pull request merge")

    handler = HandlerReviewReplaySet()
    request = _request(
        tmp_path,
        repos=(ModelReplayRepo(repo="clone", clone_path=str(repo), base_ref="main"),),
        target_count=5,
    )
    result = handler.handle(request)
    assert result.item_count == 1
    manifest = Path(result.manifest_path).read_text(encoding="utf-8")
    assert '"pr_number":7' in manifest.replace(" ", "")
    diff = next((Path(result.manifest_path).parent / "diffs").glob("*.diff"))
    assert "+three" in diff.read_text(encoding="utf-8")


def test_main_freezes_from_a_request_file_and_exits_one_when_short(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_review_replay_set_effect.__main__ import main

    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "root")
    (repo / "a.txt").write_text("x\ny\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "feat: y (#3)")

    request = _request(
        tmp_path,
        repos=(ModelReplayRepo(repo="clone", clone_path=str(repo), base_ref="main"),),
        target_count=2,
    )
    request_file = tmp_path / "request.json"
    request_file.write_text(request.model_dump_json(), encoding="utf-8")

    assert main(["--request", str(request_file)]) == 1  # one candidate, two asked
    assert '"short_by": 1' in capsys.readouterr().out
    request_file.write_text("{}", encoding="utf-8")
    assert main(["--request", str(request_file)]) == 2


def test_the_contract_names_the_command_and_the_frozen_event() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).parents[4]
            / "src/omnimarket/nodes/node_review_replay_set_effect/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    assert contract["runtime_dispatch"]["command_topic"] == (
        "onex.cmd.omnimarket.review-replay-set-freeze-requested.v1"
    )
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.review-replay-set-frozen.v1"
    )

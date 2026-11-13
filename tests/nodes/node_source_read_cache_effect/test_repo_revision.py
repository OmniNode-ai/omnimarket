# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The revision of a source that changes only by a commit is the default branch's tip in the repository's canonical clone (OMN-19998).

Golden: a clone's tip is read with one local git command and the cache's overlay says where the clone is. Error chain: an absent clone, a
failing command and an answer that is not a commit each give no revision, so a caller reads its source; and the lookup and store pair a
step uses to remember a typed absence counts hits and misses.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_source_read_cache_effect.handlers.repo_revision import (
    cached_repo_head,
    repo_head_revision,
    run_git,
)
from tests.nodes.node_source_read_cache_effect.support import memory_reader

pytestmark = pytest.mark.unit

SHA = "0123456789abcdef0123456789abcdef01234567"


def _runner(code: int, out: str) -> tuple[list[Sequence[str]], object]:
    calls: list[Sequence[str]] = []

    def run(argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        calls.append(argv)
        return code, out, ""

    return calls, run


def test_golden_the_tip_of_the_clone_is_the_revision(tmp_path: Path) -> None:
    (tmp_path / "repo-x").mkdir()
    calls, run = _runner(0, SHA + "\n")
    got = repo_head_revision(
        tmp_path,
        "Org/repo-x",
        clone_relpath_template="{name}",
        timeout_seconds=3,
        run=run,  # type: ignore[arg-type]
    )
    assert got == SHA
    assert calls[0][:3] == ["git", "-C", str(tmp_path / "repo-x")]


def test_error_chain_no_clone_a_failing_command_and_a_non_commit_give_no_revision(
    tmp_path: Path,
) -> None:
    _calls, run = _runner(0, SHA)
    assert (
        repo_head_revision(
            tmp_path,
            "Org/absent",
            clone_relpath_template="{name}",
            timeout_seconds=3,
            run=run,  # type: ignore[arg-type]
        )
        is None
    )
    (tmp_path / "present").mkdir()
    for code, out in ((128, ""), (0, "not a commit!"), (0, "")):
        _c, failing = _runner(code, out)
        assert (
            repo_head_revision(
                tmp_path,
                "Org/present",
                clone_relpath_template="{name}",
                timeout_seconds=3,
                run=failing,  # type: ignore[arg-type]
            )
            is None
        )


def test_cached_repo_head_uses_the_overlays_clone_settings_and_none_without_a_cache(
    tmp_path: Path,
) -> None:
    (tmp_path / "repo-z").mkdir()
    _calls, run = _runner(0, SHA)
    cache, _ = memory_reader()
    assert cached_repo_head(cache, tmp_path, "Org/repo-z", run) == SHA  # type: ignore[arg-type]
    assert cached_repo_head(None, tmp_path, "Org/repo-z", run) is None  # type: ignore[arg-type]


def test_the_run_git_runner_answers_a_missing_binary_as_a_failure() -> None:
    code, _out, _err = run_git(["/nonexistent/git-binary"], 2)
    assert code == 127

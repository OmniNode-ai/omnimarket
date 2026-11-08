# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-23033: a current mirror answers with no GitHub call; a stale one pulls once.

Both paths run against planted git repositories (a bare upstream standing in for GitHub and
the host's clone of it), with every GitHub-bound git command counted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_query import (
    EnumRepoStateSource,
    EnumRepoStateWant,
    ModelRepoStateQuery,
)
from tests.nodes.node_repo_state_query_effect.planted import (
    HOST,
    OTHER,
    REPO,
    git,
    plant,
)

pytestmark = pytest.mark.unit


def _q(
    want: EnumRepoStateWant = EnumRepoStateWant.BRANCH_HEAD, **kw: object
) -> ModelRepoStateQuery:
    return ModelRepoStateQuery(host=HOST, repo=REPO, want=want, **kw)


async def test_a_current_mirror_answers_from_the_mirror_with_no_github_call(
    tmp_path: Path,
) -> None:
    p = plant(tmp_path)
    p.bus_merge(p.mirror_head())  # the bus's latest state is what the mirror holds

    answer = await p.handler.handle(_q(branch="main"))

    assert answer.ok
    assert answer.current
    assert answer.source is EnumRepoStateSource.MIRROR
    assert answer.mirror_sha == p.mirror_head() == answer.bus_sha
    assert answer.comparisons == 1
    assert not answer.pulled
    assert p.handler.pulls == 0
    assert p.sync.runs == 0
    assert p.runner.github_calls == 0
    assert p.runner.commands, "the mirror was read through the counted runner"


async def test_a_stale_mirror_pulls_once_and_then_answers(tmp_path: Path) -> None:
    p = plant(tmp_path)
    old = p.mirror_head()
    new = p.push_upstream()  # GitHub moved on; the clone has not
    p.bus_merge(new)

    answer = await p.handler.handle(_q(branch="main"))

    assert answer.ok
    assert answer.current
    assert answer.source is EnumRepoStateSource.MIRROR_AFTER_PULL
    assert answer.pulled
    assert answer.pull is not None
    assert answer.pull.ok
    assert (answer.pull.before_sha, answer.pull.after_sha) == (old, new)
    assert answer.mirror_sha == new == p.mirror_head()
    assert p.handler.pulls == 1
    assert p.sync.runs == 1
    assert p.runner.github_calls == 1

    again = await p.handler.handle(_q(branch="main"))  # now current: no second pull
    assert again.source is EnumRepoStateSource.MIRROR
    assert not again.pulled
    assert p.runner.github_calls == 1


async def test_files_at_a_revision_the_mirror_lacks_pull_first(tmp_path: Path) -> None:
    p = plant(tmp_path)
    new = p.push_upstream("g.txt", "planted\n")

    answer = await p.handler.handle(
        _q(EnumRepoStateWant.FILES, revision=new, paths=("g.txt", "absent.txt"))
    )

    assert answer.ok
    assert answer.source is EnumRepoStateSource.MIRROR_AFTER_PULL
    assert [(f.path, f.present, f.content) for f in answer.files or ()] == [
        ("g.txt", True, "planted\n"),
        ("absent.txt", False, None),
    ]
    assert p.runner.github_calls == 1


async def test_refs_compare_every_branch_the_bus_knows(tmp_path: Path) -> None:
    p = plant(tmp_path)
    p.bus_merge(p.mirror_head())
    answer = await p.handler.handle(_q(EnumRepoStateWant.REFS))
    assert answer.ok
    assert answer.source is EnumRepoStateSource.MIRROR
    assert answer.refs == {"main": p.mirror_head()}
    assert p.runner.github_calls == 0


async def test_injected_break_a_sync_that_never_fetches_reads_behind(
    tmp_path: Path,
) -> None:
    """The reading can fail for real: a sync that does not advance the clone."""
    p = plant(tmp_path, fetch=False)
    new = p.push_upstream()
    p.bus_merge(new)

    answer = await p.handler.handle(_q(branch="main"))

    assert answer.source is EnumRepoStateSource.MIRROR_BEHIND
    assert answer.current is False
    assert answer.pulled
    assert answer.mirror_sha != new
    assert p.handler.pulls == 1  # one pull per query, never a second


async def test_a_current_watcher_answers_pull_request_state_with_no_refresh(
    tmp_path: Path,
) -> None:
    p = plant(tmp_path, watcher_tick=datetime.now(UTC).isoformat())
    p.bus_merge("b" * 40, pr=7, at=datetime.now(UTC) - timedelta(minutes=5))

    pr = await p.handler.handle(_q(EnumRepoStateWant.PULL_REQUEST, pr_number=7))
    checks = await p.handler.handle(_q(EnumRepoStateWant.CHECKS, pr_number=7))
    job = await p.handler.handle(
        _q(EnumRepoStateWant.JOB_LOG, pr_number=7, job_id="42")
    )

    assert all(
        a.ok and a.source is EnumRepoStateSource.MIRROR for a in (pr, checks, job)
    )
    assert checks.record == {
        "state": "green",
        "detail": [{"job_id": "42", "name": "tests", "conclusion": "success"}],
    }
    assert job.record == {"job_id": "42", "name": "tests", "conclusion": "success"}
    assert p.watcher_runs == []
    assert p.runner.github_calls == 0


async def test_a_stale_watcher_is_refreshed_once_then_answers(tmp_path: Path) -> None:
    p = plant(tmp_path, watcher_tick="2026-11-07T21:00:00Z")
    p.bus_merge("b" * 40, pr=7, at=datetime.now(UTC))

    answer = await p.handler.handle(_q(EnumRepoStateWant.PULL_REQUEST, pr_number=7))

    assert answer.ok
    assert answer.source is EnumRepoStateSource.MIRROR_AFTER_PULL
    assert p.watcher_runs == [["planted-watcher-refresh", REPO]]


async def test_error_chain_no_clone_and_wrong_host_are_refused(tmp_path: Path) -> None:
    p = plant(tmp_path)
    missing = await p.handler.handle(
        ModelRepoStateQuery(
            host=HOST, repo="OmniNode-ai/absent", want=EnumRepoStateWant.BRANCH_HEAD
        )
    )
    assert not missing.ok
    assert missing.source is EnumRepoStateSource.UNAVAILABLE
    assert "no canonical clone" in (missing.error or "")
    elsewhere = await p.handler.handle(
        ModelRepoStateQuery(
            host="h-other", repo=REPO, want=EnumRepoStateWant.BRANCH_HEAD
        )
    )
    assert not elsewhere.ok
    assert "addressed to h-other" in (elsewhere.error or "")
    assert p.runner.github_calls == 0


def test_the_fold_keeps_the_newest_event_and_survives_a_restart(tmp_path: Path) -> None:
    from omnimarket.nodes.node_repo_state_query_effect.handlers.adapter_repo_state_mirrors import (
        FoldedBusRepoState,
    )

    path = tmp_path / "fold.json"
    fold = FoldedBusRepoState(path)
    now = datetime.now(UTC)
    base = {
        "repo": OTHER,
        "base": "dev",
        "pr_number": 3,
        "source": "t",
        "state": "merged",
    }
    assert fold.apply({**base, "merge_sha": "c" * 40, "merged_at": now.isoformat()})
    assert not fold.apply(
        {
            **base,
            "merge_sha": "d" * 40,
            "merged_at": (now - timedelta(hours=1)).isoformat(),
        }
    )
    assert fold.apply(
        {
            "repo": OTHER,
            "base_ref": "dev",
            "pr_number": 4,
            "merge_sha": "e" * 40,
            "merged_at": (now + timedelta(seconds=1)).isoformat(),
        }
    )
    assert not fold.apply({"repo": "not-a-slug", "merge_sha": "f" * 40})
    restored = FoldedBusRepoState(path)
    head = restored.head(OTHER, "dev")
    assert head is not None
    assert head.sha == "e" * 40
    assert restored.pr_event(OTHER, 3) is not None


def test_query_validation_refuses_climbing_paths_and_missing_numbers() -> None:
    with pytest.raises(ValueError, match="never climb out"):
        _q(EnumRepoStateWant.FILES, revision="main", paths=("../etc/passwd",))
    with pytest.raises(ValueError, match="needs pr_number"):
        _q(EnumRepoStateWant.CHECKS)
    with pytest.raises(ValueError, match="needs job_id"):
        _q(EnumRepoStateWant.JOB_LOG, pr_number=1)
    assert git("--version").startswith("git version")

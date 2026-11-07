# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 of OMN-23033: concurrent queries for one stale repository cause one pull, not one each."""

from __future__ import annotations

import asyncio
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
N = 8


def _q(repo: str = REPO) -> ModelRepoStateQuery:
    return ModelRepoStateQuery(
        host=HOST, repo=repo, want=EnumRepoStateWant.BRANCH_HEAD, branch="main"
    )


async def test_concurrent_queries_of_one_stale_repository_share_one_pull(
    tmp_path: Path,
) -> None:
    p = plant(
        tmp_path, delay_s=0.4
    )  # a pull slow enough that every query arrives during it
    new = p.push_upstream()
    p.bus_merge(new)

    answers = await asyncio.gather(*(p.handler.handle(_q()) for _ in range(N)))

    assert p.sync.runs == 1
    assert p.handler.pulls == 1
    assert p.runner.github_calls == 1
    assert all(a.ok and a.current and a.mirror_sha == new for a in answers)
    assert all(a.source is EnumRepoStateSource.MIRROR_AFTER_PULL for a in answers)
    assert {a.pull.waiters for a in answers if a.pull} == {N}


async def test_two_stale_repositories_pull_independently(tmp_path: Path) -> None:
    p = plant(tmp_path, delay_s=0.2)
    p.bus_merge(p.push_upstream())
    gadgets = tmp_path / "clones" / "gadgets"
    work = tmp_path / "work-gadgets"
    git("clone", "--quiet", str(tmp_path / "upstream" / "gadgets.git"), str(work))
    (work / "f.txt").write_text("moved\n")
    git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "--quiet",
        "-am",
        "m",
        cwd=work,
    )
    git("push", "--quiet", "origin", "main", cwd=work)
    p.bus_merge(git("rev-parse", "HEAD", cwd=work), repo=OTHER)

    answers = await asyncio.gather(
        *(p.handler.handle(_q(r)) for r in (REPO, OTHER, REPO, OTHER))
    )

    assert p.sync.runs == 2
    assert p.runner.github_calls == 2
    assert all(a.ok and a.current for a in answers)
    assert git("rev-parse", "HEAD", cwd=gadgets) == git("rev-parse", "HEAD", cwd=work)


async def test_injected_break_without_the_in_flight_table_every_query_pulls(
    tmp_path: Path,
) -> None:
    """The falsifier is live: with the shared pull removed, N queries make N pulls."""

    class _Forgets(dict):  # type: ignore[type-arg]
        def __setitem__(
            self, key: object, value: object
        ) -> None:  # never remembers a flight
            return None

    p = plant(tmp_path, delay_s=0.2)
    p.handler._inflight = _Forgets()
    p.bus_merge(p.push_upstream())

    await asyncio.gather(*(p.handler.handle(_q()) for _ in range(N)))

    assert p.sync.runs == N

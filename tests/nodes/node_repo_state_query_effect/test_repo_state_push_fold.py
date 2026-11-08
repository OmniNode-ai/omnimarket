# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A push to a branch that is never merged moves the bus's latest state (OMN-23033).

The webhook ingress publishes a push as ``branch-ref-advanced`` on the branch-head topic. The
fold used to read merge events only, so such a push could not make a mirror read as behind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_query import (
    EnumRepoStateSource,
    EnumRepoStateWant,
    ModelRepoStateQuery,
)
from tests.nodes.node_repo_state_query_effect.planted import HOST, REPO, plant


def _push(sha: str) -> dict[str, object]:
    return {
        "kind": "branch-ref-advanced",
        "repo": REPO,
        "branch": "main",
        "sha": sha,
        "as_of": datetime.now(UTC).isoformat(),
        "github_event": "push",
    }


def _head() -> ModelRepoStateQuery:
    return ModelRepoStateQuery(
        host=HOST, repo=REPO, want=EnumRepoStateWant.BRANCH_HEAD, branch="main"
    )


async def test_control_without_the_push_event_the_mirror_reads_current(
    tmp_path: Path,
) -> None:
    p = plant(tmp_path)
    p.push_upstream()  # the branch advanced upstream; the bus has carried nothing
    answer = await p.handler.handle(_head())
    assert answer.source is EnumRepoStateSource.MIRROR
    assert answer.pulled is False  # nothing told the mirror it is behind


async def test_golden_a_push_event_makes_the_mirror_read_behind_then_pull(
    tmp_path: Path,
) -> None:
    p = plant(tmp_path)
    new = p.push_upstream()
    assert p.bus_state.apply(_push(new)) is True
    answer = await p.handler.handle(_head())
    assert answer.source is EnumRepoStateSource.MIRROR_AFTER_PULL
    assert answer.mirror_sha == new
    assert answer.current is True


async def test_error_chain_a_push_event_without_a_sha_is_skipped(
    tmp_path: Path,
) -> None:
    p = plant(tmp_path)
    bad = _push("not-a-sha")
    assert p.bus_state.apply(bad) is False
    assert p.bus_state.skipped == 1

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The open pull requests of the sweep, read from the PR watcher's state (OMN-24614).

The state's shape is the one the PR watcher writes: a ``prs`` table whose records carry ``facts`` with the repository's bare
name, the number, the head, the author, the head ref, the title, the draft flag and the state. A stale state, or one with
no ``prs`` table, is unreadable; a record missing a head, a number or an author is skipped and counted, never decided.
Injected break: a source that lists drafts when the overlay says it must not.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_review_gate_context_orchestrator.adapters.watcher_open_pull_requests import (
    WatcherOpenPullRequests,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.overlay import (
    ModelReviewGateSweepSettings,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.protocols.protocol_review_gate_sources import (
    ReviewGateSourceError,
)
from tests.unit.nodes.node_review_gate_context_orchestrator.support import (
    BOT,
    HEAD,
    NOW,
    OLD_HEAD,
)

pytestmark = pytest.mark.unit


def _settings(**over: Any) -> ModelReviewGateSweepSettings:
    base: dict[str, Any] = {
        "owner": "example-org",
        "repositories": ("repo-a", "repo-b"),
        "watcher_state_path_env": "EXAMPLE_WATCHER_STATE_PATH",
        "max_age_seconds": 900,
        "include_drafts": False,
        "lookback_days": 14,
        "review_lane_prefixes": ("orchestrator-",),
        "review_engines": ("fable",),
    }
    return ModelReviewGateSweepSettings(**{**base, **over})


def _record(**facts: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "repo": "repo-a",
        "number": 1,
        "head_sha": HEAD,
        "author": "lane-author",
        "head_ref": "omn-1-change",
        "title": "OMN-1: a change",
        "draft": False,
        "state": "OPEN",
        "base": "dev",
    }
    return {"baseline": False, "facts": {**base, **facts}}


def _state(*records: dict[str, Any], tick: datetime = NOW) -> dict[str, Any]:
    return {
        "last_tick": tick.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prs": {f"r#{i}": rec for i, rec in enumerate(records, start=1)},
    }


def _read(
    state: dict[str, Any],
    settings: ModelReviewGateSweepSettings | None = None,
    repositories: tuple[str, ...] = (),
) -> tuple[Any, WatcherOpenPullRequests]:
    source = WatcherOpenPullRequests(state, settings or _settings(), NOW)
    return source.open_pull_requests(repositories), source


def test_an_open_pull_request_is_read_with_its_author_head_ref_and_title() -> None:
    subjects, _ = _read(_state(_record(author=BOT, head_ref="automation/release-1")))
    only = subjects[0]
    assert (only.repository, only.pr_number, only.head_sha) == (
        "example-org/repo-a",
        1,
        HEAD,
    )
    assert (only.author_login, only.head_ref) == (BOT, "automation/release-1")


def test_a_closed_pull_request_and_another_repository_are_not_read() -> None:
    state = _state(
        _record(state="MERGED"),
        _record(repo="repo-z", number=2, head_sha=OLD_HEAD),
        _record(repo="repo-b", number=3),
    )
    subjects, _ = _read(state)
    assert [s.repository for s in subjects] == ["example-org/repo-b"]


def test_the_command_can_narrow_the_repositories() -> None:
    state = _state(_record(), _record(repo="repo-b", number=3))
    subjects, _ = _read(state, repositories=("repo-b",))
    assert [s.pr_number for s in subjects] == [3]


def test_drafts_are_left_out_unless_the_overlay_includes_them() -> None:
    state = _state(_record(draft=True))
    assert _read(state)[0] == ()
    assert len(_read(state, _settings(include_drafts=True))[0]) == 1


def test_break_a_source_that_lists_drafts_when_the_overlay_says_not_to() -> None:
    state = _state(_record(draft=True))
    control = _read(state, _settings(include_drafts=False))[0]
    broken = _read(state, _settings(include_drafts=True))[0]
    assert control == ()
    assert len(broken) == 1


@pytest.mark.parametrize("missing", ["head_sha", "author", "number"])
def test_a_record_missing_a_head_a_number_or_an_author_is_skipped_and_counted(
    missing: str,
) -> None:
    record = _record()
    del record["facts"][missing]
    subjects, source = _read(_state(record))
    assert subjects == ()
    assert source.skipped == 1


def test_a_stale_state_is_unreadable() -> None:
    state = _state(_record(), tick=NOW - timedelta(seconds=901))
    with pytest.raises(ReviewGateSourceError, match="past the 900 s bound"):
        _read(state)


@pytest.mark.parametrize(
    "state",
    [{"prs": {}}, {"last_tick": "2026-11-15T13:00:00Z"}, {"last_tick": "x", "prs": {}}],
)
def test_a_state_without_a_tick_or_a_prs_table_is_unreadable(
    state: dict[str, Any],
) -> None:
    with pytest.raises(ReviewGateSourceError):
        _read(state)

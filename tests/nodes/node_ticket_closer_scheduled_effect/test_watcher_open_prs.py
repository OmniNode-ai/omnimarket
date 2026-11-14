# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The watcher state as the scheduled closer's open-pull-request source (OMN-24301).

The overlay is the closeout sweep's packaged one: schema 1, ticks read for at most
``watcher_tick_max_age_seconds``. The pull requests live on a product repository, not the
evidence one the rules leave out.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    PACKAGED_OVERLAY,
    load_scope_overlay,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.watcher_open_prs import (
    WatcherOpenPullRequests,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def state_file(directory: Path, state: dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def watcher_state(tick: datetime, *, schema: int = 1) -> dict[str, Any]:
    stamp = tick.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "schema": schema,
        "repos": {"omnimarket": {}},
        "last_full_resync": stamp,
        "last_tick": stamp,
        "prs": {
            "omnimarket#5": {
                "facts": {
                    "repo": "omnimarket",
                    "number": 5,
                    "title": "OMN-1: let the closer read the watcher (#5)",
                    "head_ref": "omn-1-closer-reads",
                    "state": "OPEN",
                }
            },
            "omnimarket#6": {
                "facts": {
                    "repo": "omnimarket",
                    "number": 6,
                    "title": "OMN-2: other work (#6)",
                    "head_ref": "omn-2-other",
                    "state": "MERGED",
                }
            },
        },
    }


def source(path: Path) -> WatcherOpenPullRequests:
    return WatcherOpenPullRequests(
        path, load_scope_overlay(PACKAGED_OVERLAY), clock=lambda: NOW
    )


def test_golden_only_the_tickets_with_an_open_pull_request_answer(
    tmp_path: Path,
) -> None:
    path = state_file(tmp_path, watcher_state(NOW))
    found = source(path).open_for(frozenset({"OMN-1", "OMN-2"}))
    assert found == frozenset({"OMN-1"})
    assert source(path).open_for(frozenset({"OMN-1", "OMN-3"})) == frozenset({"OMN-1"})


def test_error_chain_a_state_that_cannot_be_read_as_current_is_unread(
    tmp_path: Path,
) -> None:
    max_age = load_scope_overlay(PACKAGED_OVERLAY).watcher_tick_max_age_seconds
    stale = watcher_state(NOW - timedelta(seconds=max_age + 60))
    cases = {
        "stale": state_file(tmp_path / "stale", stale),
        "missing": tmp_path / "missing" / "state.json",
        "wrong schema": state_file(tmp_path / "schema", watcher_state(NOW, schema=2)),
    }
    for name, path in cases.items():
        assert source(path).open_for(frozenset({"OMN-1"})) is None, name


def test_failing_control_a_stale_state_is_not_an_empty_open_set(tmp_path: Path) -> None:
    path = state_file(tmp_path, watcher_state(NOW - timedelta(hours=2)))
    found = source(path).open_for(frozenset({"OMN-1"}))
    assert found is None
    assert found != frozenset()

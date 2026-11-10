# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real backend of the landing source: the watcher's state file and the goal scope overlay (OMN-22918)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_reading import (
    EnumReadingVerdict,
    ModelReadingWindowCommand,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.landing_adapters import (
    WatcherFileLandingSource,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_reading_writer_effect.reading_support import RUN_AT, START, stamp
from tests.unit.nodes.node_reading_writer_effect.landing_support import (
    landing_overlay,
    writer,
)

pytestmark = pytest.mark.unit

REQUIREMENTS = landing_overlay().spec(9).landing
ROUTE = landing_overlay().landing_route
assert REQUIREMENTS is not None
assert ROUTE is not None


def _pr(
    *, cls: str, green_minutes_ago: int, draft: bool = False, state: str = "OPEN"
) -> dict:
    done = stamp(RUN_AT - timedelta(minutes=green_minutes_ago))
    return {
        "cls": cls,
        "facts": {
            "state": state,
            "draft": draft,
            "title": "OMN-1 a change",
            "head_ref": "omn-1-a",
            "body": "",
        },
        "ci": {"detail": [["required", "success", done]]},
        "review": {"merge_state": "CLEAN"},
    }


def _environ(
    tmp_path: Path, prs: dict, merges: dict, *, tick_minutes_ago: int = 1
) -> dict:
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "last_tick": stamp(RUN_AT - timedelta(minutes=tick_minutes_ago)),
                "prs": prs,
                "merges": merges,
            }
        ),
        encoding="utf-8",
    )
    scope = tmp_path / "scope.yaml"
    scope.write_text(
        yaml.safe_dump({"ruling": "test", "in_scope_repos": ["scoped"]}),
        encoding="utf-8",
    )
    return {
        ROUTE.watcher_state_env: str(state),
        ROUTE.goal_scope_file_env: str(scope),
    }


def _merge(minutes_ago: int, repo: str = "scoped") -> dict:
    return {
        "merged_at": stamp(RUN_AT - timedelta(minutes=minutes_ago)),
        "title": "OMN-1 merged",
        "head_ref": "omn-1-m",
    }


def test_the_snapshot_counts_only_in_scope_non_draft_green_waiting_heads(
    tmp_path: Path,
) -> None:
    prs = {
        "scoped#1": _pr(cls="green-unarmed", green_minutes_ago=75),
        "scoped#2": _pr(cls="queue-stuck", green_minutes_ago=10),
        "scoped#3": _pr(cls="green-unarmed", green_minutes_ago=90, draft=True),
        "scoped#4": _pr(cls="red", green_minutes_ago=90),
        "other#5": _pr(cls="green-unarmed", green_minutes_ago=90),
    }
    merges = {"scoped#9": _merge(20), "scoped#8": _merge(200), "other#7": _merge(5)}
    read = WatcherFileLandingSource(ROUTE, _environ(tmp_path, prs, merges)).read(
        REQUIREMENTS, RUN_AT
    )
    assert read.error is None
    assert read.snapshot is not None
    waiting = {head.ref: head.waiting_seconds for head in read.snapshot.heads}
    assert waiting == {"scoped#1": 75 * 60, "scoped#2": 10 * 60}
    assert read.snapshot.merges_in_window == 1
    assert read.snapshot.age_seconds == 60
    # OMN-23077: the watcher's last tick rides with the snapshot so a stale reason can name it.
    assert read.snapshot.newest_at == RUN_AT - timedelta(minutes=1)


def test_a_head_green_over_an_hour_makes_the_written_reading_not_working(
    tmp_path: Path,
) -> None:
    environ = _environ(
        tmp_path, {"scoped#1": _pr(cls="green-unarmed", green_minutes_ago=75)}, {}
    )
    ledger = InMemoryLedger([])
    result = writer(ledger, WatcherFileLandingSource(ROUTE, environ)).handle(
        ModelReadingWindowCommand(correlation_id="c-w", window_start=START, pieces=(9,))
    )
    (piece,) = result.pieces
    assert piece.verdict is EnumReadingVerdict.NOT_WORKING
    assert piece.reasons == ("head-unmerged-over-limit:scoped-1",)


def test_the_same_head_green_inside_the_hour_reads_working(tmp_path: Path) -> None:
    environ = _environ(
        tmp_path, {"scoped#1": _pr(cls="green-unarmed", green_minutes_ago=30)}, {}
    )
    ledger = InMemoryLedger([])
    result = writer(ledger, WatcherFileLandingSource(ROUTE, environ)).handle(
        ModelReadingWindowCommand(correlation_id="c-w", window_start=START, pieces=(9,))
    )
    assert result.pieces[0].verdict is EnumReadingVerdict.WORKING


def test_a_watcher_tick_older_than_its_bound_reads_stale(tmp_path: Path) -> None:
    environ = _environ(tmp_path, {}, {}, tick_minutes_ago=20)
    read = WatcherFileLandingSource(ROUTE, environ).read(REQUIREMENTS, RUN_AT)
    assert read.snapshot is not None
    assert read.snapshot.age_seconds == 1200


def test_a_missing_state_file_or_scope_is_an_error_and_never_an_empty_queue(
    tmp_path: Path,
) -> None:
    environ = _environ(tmp_path, {}, {})
    (tmp_path / "state.json").unlink()
    read = WatcherFileLandingSource(ROUTE, environ).read(REQUIREMENTS, RUN_AT)
    assert read.snapshot is None
    assert read.error == "the watcher state file is not read"
    unset = WatcherFileLandingSource(ROUTE, {}).read(REQUIREMENTS, RUN_AT)
    assert unset.snapshot is None
    assert "is not set" in (unset.error or "")
    environ2 = _environ(tmp_path, {}, {})
    (tmp_path / "scope.yaml").unlink()
    no_scope = WatcherFileLandingSource(ROUTE, environ2).read(REQUIREMENTS, RUN_AT)
    assert no_scope.snapshot is None
    assert "goal scope" in (no_scope.error or "")

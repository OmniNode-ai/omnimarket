# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's merge number counts the merges the landing controller admitted as in scope (OMN-22876).

The floor counted a merge in scope only when a goal list named it, so merges the landing controller admitted by its ticket rule
counted 0 (omnimarket#5243, merged 2026-11-07T10:35:57Z, was one). The controller records each verdict in its
``admissions.jsonl``; the floor reads that record, so its number reads what the controller admits.

The five merges are operator pull requests of 2026-11-07 (09:58Z to 11:00Z) on no list. The error half names its injected breaks:
the record is absent (the lists alone decide), the newest row keeps a merge out, the paused list overrides a row, and the
record cannot be read (the number is drawn unread, never a smaller count).
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.floor_17a import ModelGoalScope
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import (
    read_admissions,
    read_watcher,
)
from tests.nodes.node_floor_simple_effect.support import (
    NOW,
    build_home,
    iso,
    watcher,
)
from tests.nodes.node_floor_simple_effect.test_golden_chain_floor_simple import (
    run,
    written,
)

pytestmark = pytest.mark.unit

TONIGHT = (
    "omnimarket#5243",
    "omnibase_infra#5765",
    "omnibase_infra#5753",
    "omnibase_infra#5755",
    "omnibase_infra#5760",
)
SCOPE = ModelGoalScope(
    ruling="a test goal",
    in_scope_prs=("r#1", "r#3", "r#9"),
    paused_prs=("omnibase_infra#5766",),
)


def row(pr: str, admitted: bool = True) -> dict[str, Any]:
    return {
        "ts": "2026-11-07T10:00:00Z",
        "kind": "admission",
        "event": "admitted" if admitted else "kept_out",
        "pr": pr,
        "head": "a" * 40,
        "admitted": admitted,
        "via": "ticket" if admitted else "unlisted",
        "chain": "OMN-22802<OMN-22478" if admitted else "",
    }


def watch_with_tonight() -> dict[str, Any]:
    doc = watcher()
    for index, pr in enumerate(TONIGHT):
        doc["merges"][pr] = {
            "merged_at": iso(NOW - timedelta(minutes=5 + index)),
            "title": f"fix(OMN-228{index}0): a",
        }
    doc["merges"]["omnibase_infra#5766"] = {
        "merged_at": iso(NOW - timedelta(minutes=3)),
        "title": "chore(OMN-22863): paused",
    }
    return doc


def log(home: Path, rows: list[dict[str, Any]], tail: str = "") -> Path:
    path = home / "landing" / "admissions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows) + tail, encoding="utf-8"
    )
    return path


def reading(home: Path, admissions: Any) -> Any:
    got = read_watcher(
        home / ".claude_scratch/pr-watcher/state.json",
        NOW,
        excluded_classes=("held-excluded",),
        queue_classes=("green-unarmed", "queue-stuck"),
        merge_window_minutes=60,
        scope=SCOPE,
        admissions=admissions,
    )
    assert got is not None
    return got


def test_failing_control_without_the_record_the_five_merges_count_zero(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, watch=watch_with_tonight())
    got = reading(home, read_admissions(home / "landing" / "admissions.jsonl", 4096))
    assert got.merges_in_window == 1  # r#9 only: the lists alone, as before OMN-22876
    assert got.merges_controller == ()
    assert got.merges_outside >= len(TONIGHT)


def test_golden_the_merges_the_controller_admitted_count_in_scope(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, watch=watch_with_tonight())
    log(home, [row(pr) for pr in TONIGHT])
    got = reading(home, read_admissions(home / "landing" / "admissions.jsonl", 4096))
    assert got.merges_in_window == 1 + len(TONIGHT)
    assert got.merges_controller == tuple(sorted(TONIGHT))


def test_golden_the_floors_merge_number_reads_the_record_through_a_cycle(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, watch=watch_with_tonight())
    log(home, [row(pr) for pr in TONIGHT])
    assert run(home).ok
    merges = written(home).numbers[0]
    assert merges.key == "merges"
    assert merges.value == str(2 + len(TONIGHT))


def test_error_the_floors_merge_number_without_the_record_is_the_lists_alone(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, watch=watch_with_tonight())
    assert run(home).ok
    assert written(home).numbers[0].value == "2"


def test_error_a_kept_out_row_a_missing_row_and_the_paused_list_count_nothing(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, watch=watch_with_tonight())
    rows = [row(pr) for pr in TONIGHT[:3]]
    rows[0] = row(
        TONIGHT[0], admitted=False
    )  # the newest row keeps omnimarket#5243 out
    rows.append(row("omnibase_infra#5766"))  # a row for a paused pull request
    path = log(home, rows, tail="not json\n")
    got = reading(home, read_admissions(path, 4096))
    assert got.merges_controller == ("omnibase_infra#5753", "omnibase_infra#5765")
    assert got.merges_in_window == 1 + 2


def test_golden_the_newest_row_of_a_pull_request_wins_and_only_the_tail_is_read(
    tmp_path: Path,
) -> None:
    home = tmp_path
    path = log(home, [row("a#1", admitted=False), row("a#1"), row("b#2")])
    rows = read_admissions(path, 1_000_000)
    assert rows is not None
    assert rows["a#1"]["admitted"] is True
    tail = read_admissions(path, len(json.dumps(row("b#2"))) + 1)
    assert tail is not None
    assert set(tail) == {"b#2"}


def test_error_a_record_that_cannot_be_read_draws_the_merge_number_unread(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, watch=watch_with_tonight())
    (home / "landing" / "admissions.jsonl").mkdir(
        parents=True
    )  # a directory where the record should be
    assert read_admissions(home / "landing" / "admissions.jsonl", 4096) is None
    assert run(home).ok
    merges = written(home).numbers[0]
    assert merges.read is False
    assert merges.value == "unread"

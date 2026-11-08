# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings on the comparison node (OMN-23018): each test is red on the head that carried the finding."""

from __future__ import annotations

import json
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_history import (
    append_snapshot,
    read_history,
    value_in,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_metrics import (
    snapshot_from_factory_record,
    snapshot_from_floor_simple,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    HandlerFloorCompare,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelFloorCompareRequest,
    ModelSnapshot,
)
from tests.nodes.node_floor_compare_effect.support import (
    NOW,
    RT,
    build_home,
    served_floor,
    snap,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
STAMP = "2026-11-07T18:00:00Z"


def _record(stoppages: list[dict], bays: list[dict]) -> dict:
    return {"generated_at": STAMP, "floor": {"stoppages": stoppages, "bays": bays}}


def _run(home: Path, **kw):
    return HandlerFloorCompare(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        ModelFloorCompareRequest(home=str(home), now=NOW, **kw)
    )


# 1: an estimated cost is not archived as measured lane-minutes lost
def test_a_backfilled_estimated_cost_is_not_kept_as_a_measured_one() -> None:
    doc = _record(
        [
            {"name": "lab-idle", "occurrences": 4, "cost_minutes": 120},
            {"name": "job-exit", "occurrences": 2, "cost_minutes": 30},
        ],
        [],
    )
    values = snapshot_from_factory_record(doc, RT, source="b").values
    assert "stoppage_cost:stoppage:lab-idle" not in values
    assert values["cause:stoppage:lab-idle"] == 4
    assert values["stoppage_cost:stoppage:job-exit"] == 30


# 2: a resolved cause reads as zero in a collection that was read in full, never as unread
def test_a_resolved_cause_or_alarm_in_a_read_floor_reads_zero_not_unread() -> None:
    doc = served_floor()
    doc["causes"] = [
        c for c in doc["causes"] if not c["id"].startswith(("prs:", "lab:"))
    ]
    snapshot = snapshot_from_floor_simple(doc, RT, source="tick")
    assert snapshot is not None
    assert value_in(snapshot, "cause:prs:red") == 0.0
    assert value_in(snapshot, "cause:lab:hosts") == 0.0
    assert value_in(snapshot, "alarm:disk") == 0.0
    # controls: a collection the document does not list in full stays unread
    assert value_in(snapshot, "cause:chains:other") is None
    assert value_in(snapshot, "cause:stoppage:never-seen") is None


def test_a_floor_whose_pull_requests_were_not_read_leaves_their_causes_unread() -> None:
    doc = served_floor()
    for number in doc["numbers"]:
        if number["key"] == "open_prs":
            number["read"] = False
    doc["causes"] = [c for c in doc["causes"] if not c["id"].startswith("prs:")]
    snapshot = snapshot_from_floor_simple(doc, RT, source="tick")
    assert snapshot is not None
    assert value_in(snapshot, "cause:prs:red") is None


def test_the_tick_keeps_the_prefixes_the_floor_document_lists_in_full(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    assert _run(home).recorded
    (stored,) = read_history(home / RT.history_relpath)[0]
    assert {"cause:prs:", "cause:lab:", "alarm:"} <= set(stored.complete)


# 3: archived slots follow the live floor's partial-host and nonzero-capacity rules
def test_a_stale_bay_does_not_hide_the_slots_of_the_hosts_that_were_read() -> None:
    bays = [
        {"host": "a", "running": 3, "slots": 8},
        {"host": "b", "stale": True, "running": 0, "slots": 8},
    ]
    values = snapshot_from_factory_record(_record([], bays), RT, source="b").values
    assert values["slots_busy"] == 3


@pytest.mark.parametrize(
    "bays",
    [
        [{"host": "a", "running": 0, "slots": 0}],
        [{"host": "a", "stale": True, "running": 2, "slots": 8}],
        [],
    ],
)
def test_slots_with_no_read_capacity_are_unread(bays: list[dict]) -> None:
    values = snapshot_from_factory_record(_record([], bays), RT, source="b").values
    assert "slots_busy" not in values


# 4: an archive copy that could not be read yet is tried again, not consumed
def test_an_unreadable_dated_copy_is_imported_once_it_becomes_readable(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    copies = home / ".claude_scratch/factory-floor/history"
    copies.mkdir(parents=True)
    copy = copies / "floor-simple-20261106T183000Z.json"
    copy.write_text("{ not yet json")
    assert _run(home).backfilled == 0
    old = served_floor(NOW.replace(day=6))
    copy.write_text(json.dumps(old))
    assert _run(home).backfilled == 1
    assert _run(home).backfilled == 0


# 5: an interrupted tail keeps the next record whole
def test_an_append_after_an_interrupted_line_stays_readable(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    path.write_text('{"at": "2026-11-07T', encoding="utf-8")
    append_snapshot(path, snap(1, {"open_prs": 3}))
    snapshots, skipped = read_history(path)
    assert [s.values for s in snapshots] == [{"open_prs": 3.0}]
    assert skipped == 1


# 6: the page accepts a refresh only when it can draw it
def test_the_page_keeps_the_last_good_document_when_a_refresh_is_not_renderable() -> (
    None
):
    page = (
        ROOT
        / "src/omnimarket/nodes/node_factory_floor_publish_effect/page/compare.html"
    ).read_text(encoding="utf-8")
    assert "const renderable" in page
    assert "Number.isFinite(Date.parse(d.generated_at))" in page
    assert "doc = await r.json()" not in page
    assert page.index("if (!renderable(next))") < page.index("doc = next")


# 7: archive-only operation does not need a live floor document
def test_backfill_only_imports_the_archives_with_no_live_floor(tmp_path: Path) -> None:
    home = tmp_path / "home"
    copies = home / ".claude_scratch/factory-floor/history"
    copies.mkdir(parents=True)
    (copies / "floor-simple-20261106T183000Z.json").write_text(
        json.dumps(served_floor(NOW.replace(day=6)))
    )
    result = _run(home, backfill_only=True)
    assert result.ok, result.failure_detail
    assert result.backfilled == 1
    assert not result.recorded


def test_a_tick_with_no_live_floor_still_fails_typed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = _run(home)
    assert not result.ok
    assert result.failure_code == "simple_payload_invalid"


# 8: one timezone invariant before history arithmetic
def test_a_request_or_snapshot_without_a_timezone_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelFloorCompareRequest(now=datetime(2026, 11, 7, 18, 30))
    with pytest.raises(ValidationError):
        ModelSnapshot(at=datetime(2026, 11, 7, 18, 30), source="x", values={})
    assert ModelFloorCompareRequest(now=NOW.astimezone(UTC)).now == NOW


def test_a_history_line_without_a_timezone_is_skipped_not_mixed_in(
    tmp_path: Path,
) -> None:
    path = tmp_path / "history.jsonl"
    naive = json.dumps({"at": "2026-11-07T18:00:00", "source": "tick", "values": {}})
    path.write_text(naive + "\n", encoding="utf-8")
    append_snapshot(path, snap(1, {"open_prs": 3}))
    snapshots, skipped = read_history(path)
    assert len(snapshots) == 1
    assert skipped == 1


# 9: the declared entry point is in the installed registry
def test_the_declared_entry_point_is_in_the_package_registry() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    nodes = project["project"]["entry-points"]["onex.nodes"]
    assert (
        nodes["node_floor_compare_effect"]
        == "omnimarket.nodes.node_floor_compare_effect"
    )

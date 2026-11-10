# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the receipts feeder's gathered directory is the projection's receipts source (OMN-23427).

The feeder writes where ``node_work_state_projection_effect`` reads: the target directory is the projection contract's
``runtime.receipts`` resolution, the gathered layout matches the projection's receipts glob, and what the projection's
RECEIPTS reader returns after a feed is exactly the receipts the feeder accepted. A receipt that names its status under
another key is never gathered, a source directory nobody can read fails the feed, and receipts older than the bound are
left where they lie.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.work_state_feeder.model_receipts_feed_rules import (
    ModelReceiptsFeedRules,
)
from omnimarket.models.work_state_projection import EnumWorkStateSource
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    source_readers,
)
from omnimarket.nodes.node_work_state_receipts_feeder_scheduled_effect.protocols.adapter_receipts_feeder import (
    LaneReceiptsFeeder,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import rules

pytestmark = pytest.mark.unit

T0 = datetime.now(UTC).replace(
    microsecond=0
)  # the planted files carry the real clock, so the feed runs at it
VALID = [
    {"lane": "fillorch", "status": "done", "exit": 0},
    {"lane": "verify", "status": "running"},
    {"lane": "publish", "status": "failed", "exit": 3},
]


def _feed_rules(
    tmp_path: Path, dirs: list[Path], max_age_s: int = 3600
) -> ModelReceiptsFeedRules:
    return ModelReceiptsFeedRules(
        source_dirs=tuple(str(d) for d in dirs),
        source_glob="*/*.json",
        max_receipt_age_s=max_age_s,
        gathered_subdir="gathered",
    )


def _plant(dir: Path, name: str, doc: dict, *, mtime: datetime | None = None) -> Path:
    lane_dir = dir / doc.get("lane", "lane-x")
    lane_dir.mkdir(parents=True, exist_ok=True)
    path = lane_dir / f"{name}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def _projection_receipts(env: dict[str, str], tmp_path: Path) -> tuple[object, ...]:
    now = datetime.now(UTC)
    readers = source_readers(now, tmp_path / "home", env, None, rules())
    receipts = next(s for s in readers if s.source is EnumWorkStateSource.RECEIPTS)
    return receipts.read().items


def test_feed_gathers_fresh_receipts_the_projection_reads(tmp_path: Path) -> None:
    first, second = tmp_path / "host-a", tmp_path / "host-b"
    for index, doc in enumerate(VALID):
        _plant(first if index < 2 else second, f"run-{index}", doc)
    _plant(first, "run-malformed", {"lane": "fillorch"})
    _plant(
        second,
        "run-old",
        {"lane": "verify", "status": "done"},
        mtime=T0 - timedelta(hours=6),
    )
    target = tmp_path / "projection" / "remote-lanes"
    env = {"ONEX_REMOTE_LANE_DIR": str(target)}
    feeder = LaneReceiptsFeeder(
        _feed_rules(tmp_path, [first, second], max_age_s=3600), env
    )

    outcome = feeder.feed(T0)

    assert outcome.status == "fed"
    assert outcome.item_count == 3
    assert outcome.source_as_of is not None
    gathered = sorted(p.name for p in (target / "gathered").glob("*.json"))
    assert gathered == [
        "fillorch--run-0.json",
        "publish--run-2.json",
        "verify--run-1.json",
    ]
    items = _projection_receipts(env, tmp_path)
    assert sorted((i.lane, i.status) for i in items) == [
        ("fillorch", "done"),
        ("publish", "failed"),
        ("verify", "running"),
    ]


def test_receipt_naming_status_under_state_is_not_gathered(tmp_path: Path) -> None:
    source = tmp_path / "host-a"
    _plant(source, "run-0", {"lane": "fillorch", "status": "done"})
    _plant(source, "run-renamed", {"lane": "verify", "state": "done"})
    target = tmp_path / "projection" / "remote-lanes"
    env = {"ONEX_REMOTE_LANE_DIR": str(target)}
    feeder = LaneReceiptsFeeder(_feed_rules(tmp_path, [source]), env)

    outcome = feeder.feed(T0)

    assert outcome.status == "fed"
    assert outcome.item_count == 1
    assert not (target / "gathered" / "verify--run-renamed.json").exists()
    items = _projection_receipts(env, tmp_path)
    assert sorted((i.lane, i.status) for i in items) == [("fillorch", "done")]


def test_every_source_directory_missing_fails_the_feed(tmp_path: Path) -> None:
    env = {"ONEX_REMOTE_LANE_DIR": str(tmp_path / "projection" / "remote-lanes")}
    feeder = LaneReceiptsFeeder(
        _feed_rules(tmp_path, [tmp_path / "gone-a", tmp_path / "gone-b"]), env
    )

    outcome = feeder.feed(T0)

    assert outcome.status == "failed"
    assert outcome.item_count == 0
    assert outcome.cause == "no receipt source directory is readable"


def test_no_receipt_newer_than_the_bound_is_not_fed(tmp_path: Path) -> None:
    source = tmp_path / "host-a"
    _plant(
        source,
        "run-old",
        {"lane": "fillorch", "status": "done"},
        mtime=T0 - timedelta(hours=6),
    )
    target = tmp_path / "projection" / "remote-lanes"
    env = {"ONEX_REMOTE_LANE_DIR": str(target)}
    feeder = LaneReceiptsFeeder(_feed_rules(tmp_path, [source], max_age_s=60), env)

    outcome = feeder.feed(T0)

    assert outcome.status == "not_fed"
    assert outcome.item_count == 0
    assert "no receipt newer than the bound" in outcome.cause
    assert not (target / "gathered").exists() or not any(
        (target / "gathered").iterdir()
    )

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the linear feeder's snapshot and the projection's linear reader share one directory and one field name (OMN-23427).

node_work_state_linear_feeder_scheduled_effect writes the snapshot into the directory the projection's contract names
(``runtime.linear.dir_env``, else the home under ``runtime.linear.dir_relpath``) and the projection's LINEAR reader
globs ``runtime.linear.glob`` in that same directory and reads its ``issues`` through ``tickets_from_linear_snapshot``.
The tests fail when the two sides name different directories or different field names: a feed the reader cannot see,
or a reader that reads nothing out of a fed snapshot.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.work_state_projection import EnumWorkStateSource
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshotResult,
)
from omnimarket.nodes.node_work_state_linear_feeder_scheduled_effect.protocols.adapter_linear_feeder import (
    LinearSnapshotFeeder,
)
from omnimarket.nodes.node_work_state_projection_effect.runtime_work_state import (
    source_readers,
)
from tests.nodes.node_work_state_projection_scheduled_effect.support import rules

pytestmark = pytest.mark.unit

#: The snapshot file the fake handler writes: today's UTC date at the time the join was written.
SNAPSHOT_FILE = "linear-2026-11-10.json"
OPERATOR = "op"
ISSUES = [
    {"id": "ZEP-7", "title": "t1", "statusType": "started", "project": "Zephyr"},
    {"id": "ZEP-8", "title": "t2", "statusType": "unstarted", "project": "Zephyr"},
]


class FakeSnapshotHandler:
    """Stands in for HandlerLinearSnapshot: writes one snapshot into the request's out_dir, counts only."""

    def __init__(self, issues_key: str = "issues") -> None:
        self._issues_key = issues_key
        self.requests: list[Any] = []

    def handle(self, request: Any) -> ModelLinearSnapshotResult:
        self.requests.append(request)
        out_dir = Path(request.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / SNAPSHOT_FILE
        path.write_text(
            json.dumps({"operator": OPERATOR, self._issues_key: ISSUES}),
            encoding="utf-8",
        )
        return ModelLinearSnapshotResult(
            status="written",
            path=str(path),
            issue_count=len(ISSUES),
            with_description=0,
        )


class FakeStaleHandler(FakeSnapshotHandler):
    """A run that kept the last good snapshot: reports its age instead of writing fresh."""

    def __init__(
        self, age_s: int, reason: str = "linear refused, snapshot kept"
    ) -> None:
        super().__init__()
        self._age_s = age_s
        self._reason = reason

    def handle(self, request: Any) -> ModelLinearSnapshotResult:
        return ModelLinearSnapshotResult(
            status="stale",
            path=str(Path(request.out_dir) / SNAPSHOT_FILE),
            issue_count=2,
            with_description=0,
            age_s=self._age_s,
            reason=self._reason,
        )


class FakeFailedHandler(FakeSnapshotHandler):
    """A run that wrote nothing."""

    def __init__(self, reason: str = "rate limited") -> None:
        super().__init__()
        self._reason = reason

    def handle(self, request: Any) -> ModelLinearSnapshotResult:
        return ModelLinearSnapshotResult(
            status="failed",
            path="",
            issue_count=0,
            with_description=0,
            reason=self._reason,
        )


def _linear_read(now: datetime, home: Path, env: dict[str, str]) -> Any:
    """The projection's LINEAR reader over the fed directory, under the projection's own rules."""
    read = next(
        spec.read
        for spec in source_readers(now, home, env, None, rules())
        if spec.source is EnumWorkStateSource.LINEAR
    )
    return read()


def test_golden_one_feed_puts_two_tickets_where_the_reader_globs(tmp_path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    env = {"OMNI_HOME": str(tmp_path)}
    handler = FakeSnapshotHandler()
    outcome = LinearSnapshotFeeder(environ=env, handler=handler).feed(now)
    assert outcome.status == "fed"
    assert outcome.source_as_of == now
    assert outcome.item_count == 2
    assert [r.out_dir for r in handler.requests] == [
        str(tmp_path / ".onex_state" / "linear-snapshot")
    ]
    read = _linear_read(now, tmp_path, env)
    assert [t.ticket_id for t in read.items] == ["ZEP-7", "ZEP-8"]


def test_error_an_issues_list_under_a_renamed_key_reads_as_no_tickets(tmp_path) -> None:
    """The failing control of the golden above: the projection reads the snapshot's ``issues`` key, not any key."""
    now = datetime.now(UTC).replace(microsecond=0)
    env = {"OMNI_HOME": str(tmp_path)}
    outcome = LinearSnapshotFeeder(
        environ=env, handler=FakeSnapshotHandler(issues_key="tickets")
    ).feed(now)
    assert outcome.status == "fed"
    read = _linear_read(now, tmp_path, env)
    assert not read.items


def test_stale_and_failed_runs_name_the_newest_fact_and_the_cause(tmp_path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    env = {"OMNI_HOME": str(tmp_path)}
    stale = LinearSnapshotFeeder(environ=env, handler=FakeStaleHandler(7200)).feed(now)
    assert stale.status == "fed"
    assert stale.source_as_of == now - timedelta(hours=2)
    failed = LinearSnapshotFeeder(
        environ=env, handler=FakeFailedHandler("rate limited")
    ).feed(now)
    assert failed.status == "failed"
    assert failed.item_count == 0
    assert failed.cause == "rate limited"
    unnamed = LinearSnapshotFeeder(
        environ=env, handler=FakeFailedHandler(reason="")
    ).feed(now)
    assert unnamed.cause == "linear snapshot failed"

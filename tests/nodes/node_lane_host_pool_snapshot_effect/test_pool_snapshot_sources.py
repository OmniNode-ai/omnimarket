# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real sources of the lane host pool snapshot writer, driven by scripted commands and files (OMN-23664).

Golden: the advertisement list the owner node's model dumps parses back field for field; the ledger's live CLAIM rows count per host;
the token pool's own status result splits into usable and limited labels; the lab host table file reads.
Error: a non-list, a failing or unlaunchable command, an unreadable ledger and an unreadable table end in PoolSnapshotSourceError (the
accounts source names the cause per host instead of failing the pass); a CLAIM closed by a TERMINAL, or quiet past the window, is no lane.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_pool import (
    ModelClaudeTokenPoolEntry,
    ModelClaudeTokenPoolResult,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_sources import (
    AdvertisementCommandSource,
    AdvertisementFileSource,
    LabHostTableSource,
    LedgerRunningLanesSource,
    TokenPoolCommandAccountsSource,
    parse_advertisements,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.snapshot_errors import (
    PoolSnapshotSourceError,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models import (
    ModelAccountsRoute,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    T0,
    advertisement,
    host_row,
)

pytestmark = pytest.mark.unit


def test_parse_keeps_declared_keys_drops_unknown_and_skips_an_invalid_row() -> None:
    good = json.loads(advertisement("h1").model_dump_json())
    text = json.dumps(
        [
            {**good, "future_field": 1},
            {"host_name": "h2"},
            "not a row",
        ]
    )
    parsed = parse_advertisements(text)
    assert parsed == (advertisement("h1"),)


def test_error_parse_refuses_a_non_list_and_non_json() -> None:
    with pytest.raises(PoolSnapshotSourceError, match="list"):
        parse_advertisements("{}")
    with pytest.raises(PoolSnapshotSourceError, match="JSON"):
        parse_advertisements("<html>")


def test_advertisement_file_source_reads_and_refuses_a_missing_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ads.json"
    path.write_text(json.dumps([json.loads(advertisement("h1").model_dump_json())]))
    assert AdvertisementFileSource(path).read_advertisements() == (advertisement("h1"),)
    with pytest.raises(PoolSnapshotSourceError, match="unreadable"):
        AdvertisementFileSource(tmp_path / "absent.json").read_advertisements()


def test_advertisement_command_source_reads_stdout_and_names_a_failed_run() -> None:
    ads_json = f"[{advertisement('h1').model_dump_json()}]"
    calls: list[tuple[Sequence[str], int]] = []

    def good(argv: Sequence[str], timeout: int) -> tuple[int, str]:
        calls.append((argv, timeout))
        return 0, ads_json

    def bad(argv: Sequence[str], timeout: int) -> tuple[int, str]:
        return 1, "boom\nTopicAuthorizationFailedError: topic"

    source = AdvertisementCommandSource(("onex", "lab-work"), 40, good)
    assert source.read_advertisements() == (advertisement("h1"),)
    assert calls == [(("onex", "lab-work"), 40)]
    with pytest.raises(
        PoolSnapshotSourceError, match=r"rc=1.*TopicAuthorizationFailedError"
    ):
        AdvertisementCommandSource(("onex",), 40, bad).read_advertisements()


def test_advertisement_command_source_names_an_unlaunchable_command() -> None:
    source = AdvertisementCommandSource(("/nonexistent/onex-binary",), 5)
    with pytest.raises(PoolSnapshotSourceError, match="not run"):
        source.read_advertisements()


class ScriptedRows:
    """A ledger row source holding rows per type, as the reading writer's file and command routes answer; ``error`` makes it unreadable."""

    def __init__(
        self, rows: Sequence[ModelLedgerEvidenceRow] = (), error: str | None = None
    ) -> None:
        self._rows = tuple(rows)
        self._error = error
        self.queries: list[ModelLedgerQuery] = []

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        self.queries.append(query)
        if self._error is not None:
            return ModelLedgerRead(error=self._error)
        return ModelLedgerRead(
            rows=tuple(
                r
                for r in self._rows
                if r.row_type == query.row_type
                and query.since <= r.stamp <= query.until
            )
        )


def _row(
    minutes_ago: int, row_type: str, lane: str, **cells: str
) -> ModelLedgerEvidenceRow:
    stamp = T0 - timedelta(minutes=minutes_ago)
    return ModelLedgerEvidenceRow(
        stamp=stamp,
        row_type=row_type,
        lane=lane,
        cells=cells,
        raw=f"{stamp.isoformat()} | {row_type} | lane={lane}",
    )


def test_ledger_lanes_count_open_claims_per_host_and_skip_closed_old_and_hostless() -> (
    None
):
    rows = [
        _row(30, "CLAIM", "lane-a", host="h1"),
        _row(30, "CLAIM", "lane-b", host="h1"),
        _row(10, "TERMINAL", "lane-b"),
        _row(20, "CLAIM", "lane-c", host="h2"),
        _row(60 * 12, "CLAIM", "lane-old", host="h2"),
        _row(25, "CLAIM", "lane-hostless"),
        _row(40, "CLAIM", "lane-d", host="h3"),
        _row(5, "RELEASE", "lane-d"),
    ]
    source = ScriptedRows(rows)
    counts = LedgerRunningLanesSource(source, 6).running_lanes(T0)
    # lane-a and lane-c run; lane-b ended with a TERMINAL and lane-d with a RELEASE; lane-old claimed 12 hours ago, past the 6 hour
    # lookback, so its row is never read; lane-hostless names no host.
    assert counts == {"h1": 1, "h2": 1}
    assert {q.row_type for q in source.queries} == {"CLAIM", "TERMINAL", "RELEASE"}
    assert all(
        q.until > T0 and q.since == T0 - timedelta(hours=6) for q in source.queries
    )
    # the bus read command refuses a query with no correlation id
    assert all(UUID(q.correlation_id or "") for q in source.queries)


def test_ledger_lanes_count_a_reclaimed_lane_after_its_earlier_terminal() -> None:
    rows = [
        _row(60, "CLAIM", "lane-a", host="h1"),
        _row(50, "TERMINAL", "lane-a"),
        _row(20, "CLAIM", "lane-a", host="h2"),
    ]
    assert LedgerRunningLanesSource(ScriptedRows(rows), 6).running_lanes(T0) == {
        "h2": 1
    }


def test_ledger_lanes_refuse_an_unreadable_ledger() -> None:
    source = ScriptedRows(error="reader timed out")
    with pytest.raises(PoolSnapshotSourceError, match="unread: reader timed out"):
        LedgerRunningLanesSource(source, 6).running_lanes(T0)


def _status(host: str, *tokens: ModelClaudeTokenPoolEntry) -> str:
    return ModelClaudeTokenPoolResult(
        operation="status", host=host, tokens=tokens, status_line="pool status"
    ).model_dump_json()


def _entry(label: str, *, limited: bool = False, revoked: bool = False):
    return ModelClaudeTokenPoolEntry(
        label=label,
        fingerprint="f" * 8,
        added_at="2026-11-01T00:00:00Z",
        limited=limited,
        revoked=revoked,
    )


ROUTE = ModelAccountsRoute(
    local_argv=("$OMNI_POOL_TEST_PY", "status"),
    remote_argv=("ssh", "{target}", "status"),
    timeout_seconds=20,
)


def test_accounts_split_usable_from_limited_and_revoked_and_never_contact_an_away_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_POOL_TEST_PY", "/venv/python")
    calls: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str], timeout: int) -> tuple[int, str]:
        calls.append(tuple(argv))
        return 0, _status(
            "h1",
            _entry("acct-a"),
            _entry("acct-b", limited=True),
            _entry("acct-c", revoked=True),
        )

    rows = (
        host_row("h1", local=True),
        host_row("h2"),
        host_row("h3", available=False, unavailable_reason="repair"),
    )
    got = {
        a.host: a
        for a in TokenPoolCommandAccountsSource(ROUTE, runner).read_accounts(rows)
    }
    assert got["h1"].usable == ("acct-a",)
    assert got["h1"].limited == ("acct-b", "acct-c")
    assert got["h3"].unread_reason == "away"
    # the local row runs the local argv with its environment expanded; the remote row substitutes its ssh target; the away row is not run.
    assert calls == [("/venv/python", "status"), ("ssh", "h2", "status")]


def test_error_accounts_name_the_cause_per_host_and_do_not_fail_the_pass() -> None:
    def runner(argv: Sequence[str], timeout: int) -> tuple[int, str]:
        if argv[1] == "h1":
            return 255, "ssh: connect to host h1 port 22: Connection refused"
        if argv[1] == "h2":
            return 0, "not json"
        raise PoolSnapshotSourceError("command timed out after 20s: ssh")

    got = {
        a.host: a
        for a in TokenPoolCommandAccountsSource(ROUTE, runner).read_accounts(
            (host_row("h1"), host_row("h2"), host_row("h3"))
        )
    }
    assert "rc=255" in (got["h1"].unread_reason or "")
    assert got["h2"].unread_reason == "token pool output not JSON"
    assert "timed out" in (got["h3"].unread_reason or "")
    assert all(a.usable == () for a in got.values())


def test_lab_host_table_source_reads_the_pinned_table_and_refuses_an_unreadable_one(
    tmp_path: Path,
) -> None:
    table = tmp_path / "hosts.yaml"
    table.write_text(
        "hosts:\n  - name: h1\n    target: h1\n  - name: h2\n    target: h2\n    available: false\n",
        encoding="utf-8",
    )
    rows = LabHostTableSource({"ONEX_LAB_RUN_HOSTS": str(table)}).read_hosts()
    assert [(r.name, r.available) for r in rows] == [("h1", True), ("h2", False)]
    with pytest.raises(PoolSnapshotSourceError, match="host table"):
        LabHostTableSource(
            {"ONEX_LAB_RUN_HOSTS": str(tmp_path / "absent.yaml")}
        ).read_hosts()

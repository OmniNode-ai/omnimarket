# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census input: one bounded, dated read of the PR watcher snapshot.

Missing, stale or partial evidence stays unknown; the reader never polls GitHub
and never turns an unreadable or omitted record into a clean zero.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, cast

import pytest
from pydantic import JsonValue

from omnimarket.nodes.node_morning_ground_state_orchestrator.handlers.handler_morning_ground_state import (
    WATCHER_STATE_ENV,
    HandlerMorningGroundState,
)

from .helpers import FixtureGateway, public_overlay, request

NOW = datetime(2026, 10, 10, 12, 1, tzinfo=UTC)


def watcher_fixture(
    *,
    last_tick: str = "2026-10-10T12:00:00Z",
    last_full_resync: str = "2026-10-10T11:00:00Z",
    open_prs: bool = True,
    ci_sha: str | None = "head",
    ci_read_at: str = "2026-10-10T12:00:00Z",
    queued: bool | None = False,
    check_id: str = "123",
    check_started: str = "2026-10-10T10:00:00Z",
    with_detail: bool = True,
) -> dict[str, JsonValue]:
    record: dict[str, JsonValue] = {
        "facts": {
            "repo": "repo",
            "number": 1,
            "state": "OPEN",
            "title": "one",
            "head_sha": "head",
            "armed": True,
        },
    }
    if queued is not None:
        record["queued"] = queued
    if ci_sha is not None:
        ci: dict[str, JsonValue] = {
            "sha": ci_sha,
            "verdict": "PENDING",
            "read_at": ci_read_at,
            "runs": [["ci", "queued", "", "2026-10-10T10:00:00Z"]],
        }
        if with_detail:
            ci["detail"] = [["ci", check_id, check_started]]
        record["ci"] = ci
    return {
        "schema": 1,
        "operator": "operator",
        "repos": {"repo": {"default_branch": "main"}},
        "last_tick": last_tick,
        "last_full_resync": last_full_resync,
        "prs": {"repo#1": record} if open_prs else {},
    }


def point_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str | bytes
) -> Path:
    path = tmp_path / "state.json"
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)
    monkeypatch.setenv(WATCHER_STATE_ENV, str(path))
    return path


def first_row(result: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return cast(list[dict[str, JsonValue]], result["rows"])[0]


def test_fresh_snapshot_is_read_once_and_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = point_at(tmp_path, monkeypatch, json.dumps(watcher_fixture()))
    reads = 0
    original = Path.open

    def counted_open(self: Path, mode: str = "r") -> IO[bytes]:
        nonlocal reads
        if self == path:
            reads += 1
        return cast(IO[bytes], original(self, mode))

    monkeypatch.setattr(Path, "open", counted_open)
    result = HandlerMorningGroundState._pr_inventory(NOW)
    assert result["status"] == "FRESH"
    assert reads == 1
    assert result["total_open"] == 1
    assert result["omitted"] == 0
    row = first_row(result)
    assert row["pr"] == "repo#1"
    assert row["armed"] is True
    assert row["queued"] is False
    assert row["armed_not_queued"] is True
    assert row["ci_verdict"] == "PENDING"
    assert row["pending_checks"] == [
        {
            "name": "ci",
            "status": "queued",
            "id": "123",
            "started_at": "2026-10-10T10:00:00Z",
            "age_seconds": 7260,
        }
    ]


def test_unset_pointer_is_unreadable_not_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(WATCHER_STATE_ENV, raising=False)
    result = HandlerMorningGroundState._pr_inventory(NOW)
    assert result["status"] == "UNREADABLE"
    assert WATCHER_STATE_ENV in str(result["reason"])
    assert result["rows"] == []
    assert result["total_open"] is None


@pytest.mark.parametrize(
    "fault", ["missing", "invalid", "stale", "full-stale", "future", "empty"]
)
def test_unreadable_or_stale_snapshot_harvests_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    fixtures = {
        "stale": watcher_fixture(last_tick="2026-10-10T11:52:59Z"),
        "full-stale": watcher_fixture(last_full_resync="2026-10-10T10:29:59Z"),
        "future": watcher_fixture(last_tick="2026-10-10T12:01:01Z"),
        "empty": watcher_fixture(open_prs=False),
    }
    path = tmp_path / "state.json"
    if fault == "invalid":
        path.write_text("[]")
    elif fault != "missing":
        path.write_text(json.dumps(fixtures[fault]))
    monkeypatch.setenv(WATCHER_STATE_ENV, str(path))
    result = HandlerMorningGroundState._pr_inventory(NOW)
    assert result["status"] in {"STALE", "UNREADABLE"}
    assert result["reason"]
    assert result["rows"] == []
    assert result["total_open"] is None


@pytest.mark.parametrize("fault", ["wrong-head", "old-ci", "absent-ci", "absent-queue"])
def test_partial_evidence_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    raw = {
        "wrong-head": watcher_fixture(ci_sha="old-head"),
        "old-ci": watcher_fixture(ci_read_at="2026-10-10T11:52:59Z"),
        "absent-ci": watcher_fixture(ci_sha=None),
        "absent-queue": watcher_fixture(queued=None),
    }[fault]
    point_at(tmp_path, monkeypatch, json.dumps(raw))
    result = HandlerMorningGroundState._pr_inventory(NOW)
    assert result["status"] == "FRESH"
    row = first_row(result)
    if fault == "absent-queue":
        assert row["queued"] is None
        assert row["armed_not_queued"] is None
    else:
        assert row["ci_verdict"] == "UNKNOWN"
        assert row["pending_checks"] is None


def test_row_bound_reports_omissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    point_at(tmp_path, monkeypatch, json.dumps(watcher_fixture()))
    result = HandlerMorningGroundState._pr_inventory(NOW, max_rows=0)
    assert result["total_open"] == 1
    assert result["omitted"] == 1
    assert result["rows"] == []


@pytest.mark.parametrize("fault", ["no-start", "future-start", "no-id", "no-detail"])
def test_queued_age_needs_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    raw = {
        "no-start": watcher_fixture(check_started=""),
        "future-start": watcher_fixture(check_started="2026-10-10T12:02:00Z"),
        "no-id": watcher_fixture(check_id=""),
        "no-detail": watcher_fixture(with_detail=False),
    }[fault]
    point_at(tmp_path, monkeypatch, json.dumps(raw))
    row = first_row(HandlerMorningGroundState._pr_inventory(NOW))
    if fault == "no-detail":
        assert row["pending_checks"] is None
    else:
        checks = cast(list[dict[str, JsonValue]], row["pending_checks"])
        assert checks[0]["age_seconds"] is None


def test_oversized_snapshot_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    point_at(tmp_path, monkeypatch, b" " * (16 * 1024 * 1024 + 1))
    result = HandlerMorningGroundState._pr_inventory(NOW)
    assert result["status"] == "UNREADABLE"
    assert result["rows"] == []


class CaptureGateway(FixtureGateway):
    """Remembers which phase prompts were sent."""


def test_inventory_reaches_the_ground_state_phase_and_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC).isoformat()
    raw = watcher_fixture(last_tick=now, last_full_resync=now, ci_read_at=now)
    point_at(tmp_path, monkeypatch, json.dumps(raw))
    gateway = CaptureGateway()
    result = asyncio.run(
        HandlerMorningGroundState(None, gateway, public_overlay()).handle(
            request({"force": True})
        )
    )
    assert result.pr_inventory
    assert result.pr_inventory["status"] == "FRESH"
    marker = "PR WATCHER INVENTORY (one bounded snapshot):\n"
    supplied = json.loads(gateway.prompts["ground-state"].split(marker)[1])
    assert supplied == result.pr_inventory
    # Only the ground-state phase carries the census input.
    assert all(
        marker not in p for k, p in gateway.prompts.items() if k != "ground-state"
    )


def test_unreadable_inventory_is_still_the_terminal_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(WATCHER_STATE_ENV, raising=False)
    gateway = FixtureGateway()
    result = asyncio.run(
        HandlerMorningGroundState(None, gateway, public_overlay()).handle(
            request({"force": True})
        )
    )
    assert result.pr_inventory is not None
    assert result.pr_inventory["status"] == "UNREADABLE"

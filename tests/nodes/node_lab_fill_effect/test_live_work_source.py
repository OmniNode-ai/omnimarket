# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab fill reads its work from live state in goal order, never from a stored list.

The approved-work list the fill read was deleted by a repository retirement and nothing replaced it, so every pass
refused. The fill now reads three declared sources in this order: the integration ledger's NOT RUNNING lines, the
red pull requests of the PR watcher's state, and the open tickets of the Linear snapshot. A source that is declared and
absent, stale or unreadable refuses the pass with the source named; it never becomes an empty pass.

The ledger fixture is five recorded lines of the integration ledger (a copy taken 2026-11-12); the watcher record is the
recorded record of a red pull request. Neither holds a secret.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_lab_fill_effect.models.model_lab_fill_work_sources import (
    ModelLabFillWatcherSource,
    ModelLabFillWorkSource,
    ModelLabFillWorkSources,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_live_work import (
    LiveWorkRows,
    LiveWorkSourceError,
    read_goal_tickets,
    read_integration_ledger,
    read_live_work,
    read_red_prs,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "live_work"
NOW = 1_795_000_000.0  # fixed epoch seconds; each file's mtime is set against it
BOUND_S = 3600

# A recorded watcher record of a red pull request (facts and CI verdict as the watcher writes them).
RED_PR_FACTS = {
    "draft": False,
    "head_ref": "omn-24419-lab-fill-refusals-name-their-input",
    "head_sha": "0d65d44ab7d1f6501d6680177e2b8cab1535660b",
    "labels": [],
    "number": 6564,
    "repo": "omnimarket",
    "state": "OPEN",
    "title": "OMN-24419: name the missing overlay and file in lab-fill refusal lines",
    "updated_at": "2026-11-21T19:22:30Z",
}


def _stamp(epoch: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ledger_source(tmp_path: Path, *, age_s: float = 60) -> ModelLabFillWorkSource:
    path = tmp_path / "integration-ledger.md"
    path.write_text(
        (FIXTURES / "integration_ledger_not_running.md").read_text("utf-8"),
        encoding="utf-8",
    )
    os.utime(path, (NOW - age_s, NOW - age_s))
    return ModelLabFillWorkSource(path=str(path), max_age_seconds=BOUND_S)


def watcher_source(
    tmp_path: Path, *, verdict: str = "RED", tick_age_s: float = 60
) -> ModelLabFillWatcherSource:
    path = tmp_path / "watcher-state.json"
    state: dict[str, Any] = {
        "schema": 1,
        "operator": "operator-login",
        "repos": ["omnimarket"],
        "last_tick": _stamp(NOW - tick_age_s),
        "last_full_resync": _stamp(NOW - 600),
        "prs": {
            "omnimarket#6564": {
                "facts": RED_PR_FACTS,
                "ci": {"verdict": verdict, "sha": RED_PR_FACTS["head_sha"]},
                "queued": False,
            },
            "omnimarket#6565": {
                "facts": {
                    **RED_PR_FACTS,
                    "number": 6565,
                    "title": "OMN-24500: a green change",
                    "head_sha": "a" * 40,
                },
                "ci": {"verdict": "GREEN", "sha": "a" * 40},
                "queued": False,
            },
        },
    }
    path.write_text(json.dumps(state), encoding="utf-8")
    return ModelLabFillWatcherSource(
        path=str(path), max_tick_age_seconds=300, max_resync_age_seconds=3600
    )


def tickets_source(
    tmp_path: Path, *, age_s: float = 60, status_type: str = "started"
) -> ModelLabFillWorkSource:
    path = tmp_path / "linear-snapshot.json"
    issue = {
        "id": "OMN-9001",
        "title": "The lab fill finds its work",
        "status": "In Progress",
        "statusType": status_type,
        "assignee": "operator-id",
        "updatedAt": "2026-11-22T14:00:00Z",
    }
    path.write_text(
        json.dumps({"operator": "operator-id", "issues": [issue]}), encoding="utf-8"
    )
    os.utime(path, (NOW - age_s, NOW - age_s))
    return ModelLabFillWorkSource(path=str(path), max_age_seconds=BOUND_S)


def test_the_ledgers_not_running_lines_with_a_ticket_and_no_owner_become_rows(
    tmp_path: Path,
) -> None:
    read = read_integration_ledger(ledger_source(tmp_path), now=NOW)
    assert read.source == "integration-ledger"
    assert [(r["ticket"], r["repo"]) for r in read.rows] == [
        ("OMN-19998", "overlays"),
        ("OMN-22478", "overlays"),
        ("OMN-22528", "omnimarket"),
    ]
    # the line that names no ticket and the line another lane owns are counted, not silently dropped
    assert read.skipped == {"no-ticket": 1, "owned": 1}
    first = read.rows[0]
    assert first["kind"] == "wiring"
    assert "\n" not in str(first["goal"])
    assert str(first["goal"]).strip()
    assert str(first["acceptance_check"]).strip()


def test_a_missing_integration_ledger_refuses_with_the_source_named(
    tmp_path: Path,
) -> None:
    """Failing control: a reader that returned no rows for an absent file would pass an empty pass as green."""
    source = ModelLabFillWorkSource(
        path=str(tmp_path / "absent.md"), max_age_seconds=BOUND_S
    )
    with pytest.raises(LiveWorkSourceError) as refusal:
        read_integration_ledger(source, now=NOW)
    assert refusal.value.source == "integration-ledger"
    assert "FileNotFoundError" in refusal.value.reason


def test_an_integration_ledger_older_than_its_bound_refuses(tmp_path: Path) -> None:
    source = ledger_source(tmp_path, age_s=BOUND_S + 1)
    with pytest.raises(LiveWorkSourceError) as refusal:
        read_integration_ledger(source, now=NOW)
    assert refusal.value.source == "integration-ledger"
    assert "older than" in refusal.value.reason


def test_a_ledger_with_no_not_running_section_refuses(tmp_path: Path) -> None:
    path = tmp_path / "ledger.md"
    path.write_text("## SEEN RUNNING (1)\n- omnimarket 1 | x | OMN-1\n", "utf-8")
    os.utime(path, (NOW, NOW))
    with pytest.raises(LiveWorkSourceError) as refusal:
        read_integration_ledger(
            ModelLabFillWorkSource(path=str(path), max_age_seconds=BOUND_S), now=NOW
        )
    assert "NOT RUNNING" in refusal.value.reason


def test_a_red_goal_pull_request_becomes_a_row_and_a_green_one_does_not(
    tmp_path: Path,
) -> None:
    read = read_red_prs(watcher_source(tmp_path), ("omnimarket",), now=NOW)
    assert read.source == "pr-watcher"
    assert [(r["ticket"], r["pr"], r["kind"]) for r in read.rows] == [
        ("OMN-24419", "omnimarket#6564", "pr-red")
    ]


def test_a_pull_request_outside_the_goal_repositories_is_not_work(
    tmp_path: Path,
) -> None:
    read = read_red_prs(watcher_source(tmp_path), ("omnibase_infra",), now=NOW)
    assert read.rows == ()


def test_an_absent_watcher_state_refuses_with_the_source_named(tmp_path: Path) -> None:
    source = ModelLabFillWatcherSource(
        path=str(tmp_path / "absent.json"),
        max_tick_age_seconds=300,
        max_resync_age_seconds=3600,
    )
    with pytest.raises(LiveWorkSourceError) as refusal:
        read_red_prs(source, ("omnimarket",), now=NOW)
    assert refusal.value.source == "pr-watcher"


def test_a_stale_watcher_state_refuses(tmp_path: Path) -> None:
    with pytest.raises(LiveWorkSourceError) as refusal:
        read_red_prs(watcher_source(tmp_path, tick_age_s=900), ("omnimarket",), now=NOW)
    assert refusal.value.source == "pr-watcher"


def test_an_open_goal_ticket_becomes_a_row_and_a_closed_one_does_not(
    tmp_path: Path,
) -> None:
    read = read_goal_tickets(tickets_source(tmp_path), now=NOW)
    assert read.source == "goal-tickets"
    assert [(r["ticket"], r["kind"]) for r in read.rows] == [("OMN-9001", "ticket")]
    closed = read_goal_tickets(
        tickets_source(tmp_path, status_type="completed"), now=NOW
    )
    assert closed.rows == ()


def test_a_goal_ticket_snapshot_older_than_its_bound_refuses(tmp_path: Path) -> None:
    with pytest.raises(LiveWorkSourceError) as refusal:
        read_goal_tickets(tickets_source(tmp_path, age_s=BOUND_S + 1), now=NOW)
    assert refusal.value.source == "goal-tickets"


def test_the_sources_are_read_in_goal_order(tmp_path: Path) -> None:
    sources = ModelLabFillWorkSources(
        integration_ledger=ledger_source(tmp_path),
        pr_watcher=watcher_source(tmp_path),
        goal_tickets=tickets_source(tmp_path),
    )
    reads = read_live_work(sources, ("omnimarket", "overlays"), now=NOW)
    assert [r.source for r in reads] == [
        "integration-ledger",
        "pr-watcher",
        "goal-tickets",
    ]
    rows = [row for read in reads for row in read.rows]
    # one row per ticket: a ticket two sources name keeps its first, higher-goal row
    assert len({r["id"] for r in rows}) == len(rows)
    assert [r["kind"] for r in rows][:3] == ["wiring", "wiring", "wiring"]


def test_one_unreadable_declared_source_refuses_the_whole_read(tmp_path: Path) -> None:
    sources = ModelLabFillWorkSources(
        integration_ledger=ledger_source(tmp_path),
        pr_watcher=ModelLabFillWatcherSource(
            path=str(tmp_path / "absent.json"),
            max_tick_age_seconds=300,
            max_resync_age_seconds=3600,
        ),
        goal_tickets=None,
    )
    with pytest.raises(LiveWorkSourceError) as refusal:
        read_live_work(sources, ("omnimarket",), now=NOW)
    assert refusal.value.source == "pr-watcher"


def test_a_declaration_with_no_source_is_refused() -> None:
    with pytest.raises(ValueError, match="at least one"):
        ModelLabFillWorkSources(
            integration_ledger=None, pr_watcher=None, goal_tickets=None
        )


def test_the_rows_port_answers_depth_list_and_one_row_by_id(tmp_path: Path) -> None:
    read = read_red_prs(watcher_source(tmp_path), ("omnimarket",), now=NOW)
    port = LiveWorkRows(read.rows)
    assert port.depth("ignored") == 1
    assert [r["id"] for r in port.list_rows("ignored")] == [read.rows[0]["id"]]
    assert port.row("ignored", str(read.rows[0]["id"]))["ticket"] == "OMN-24419"
    with pytest.raises(Exception, match="missing or duplicate"):
        port.row("ignored", "no-such-row")


def test_a_declared_directory_reads_its_newest_dated_snapshot(tmp_path: Path) -> None:
    directory = tmp_path / "linear"
    directory.mkdir()
    older = directory / "linear-snapshot-2026-11-20.json"
    newer = directory / "linear-snapshot-2026-11-22.json"
    state = directory / "linear-snapshot-state.json"
    for path, ticket in ((older, "OMN-1"), (newer, "OMN-2")):
        path.write_text(
            json.dumps(
                {
                    "operator": "operator-id",
                    "issues": [{"id": ticket, "title": "t", "statusType": "started"}],
                }
            ),
            encoding="utf-8",
        )
    state.write_text("{}", encoding="utf-8")
    os.utime(older, (NOW - 500, NOW - 500))
    os.utime(newer, (NOW - 60, NOW - 60))
    os.utime(
        state, (NOW, NOW)
    )  # the state file is newer than both and is not a snapshot
    read = read_goal_tickets(
        ModelLabFillWorkSource(path=str(directory), max_age_seconds=BOUND_S), now=NOW
    )
    assert [r["ticket"] for r in read.rows] == ["OMN-2"]


def test_an_empty_snapshot_directory_refuses(tmp_path: Path) -> None:
    with pytest.raises(LiveWorkSourceError) as refusal:
        read_goal_tickets(
            ModelLabFillWorkSource(path=str(tmp_path), max_age_seconds=BOUND_S),
            now=NOW,
        )
    assert refusal.value.source == "goal-tickets"

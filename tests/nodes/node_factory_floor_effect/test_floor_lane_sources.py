# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The machine-bay and live-lane sources (OMN-21733): each parses what the real file holds, and none that is absent is an error."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    load_cloud_accounts,
    load_host_table,
    load_linear,
    load_placed,
    load_pool,
    load_receipts,
    load_runner_fleet,
    load_watcher,
    merge_linear_titles,
    parse_ledger_text,
    parse_pool_reading,
    resolve_dir,
    split_reading,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 31, 18, 0, 0, tzinfo=UTC)

# The readings exactly as the runner printed them on 2026-10-31 (hosts-probe/latest.json).
H200 = (
    "h200:cores=24,load1=19.9,busy=18.0,idle=25.0%,free_gb=98.7,si=0KB/s,so=0KB/s,pressure=1,memfree=84%,memtotal=192.0G,"
    "coauthor=false,langrant=unknown,claudepool=[claude-pool host=studio tokens=8 limited=0 next_reset=none revoked=a,b],"
    "hookparse=ok,gh=ok/cached,codex=login/ok,placed=0,cap=12,slots=0,login=claude,lane_cap=12,lane_slots=0,"
    "LANE-REFUSED(CPU-IDLE-UNDER-BAR: 25.0% idle < 25%)"
)
H201 = (
    "h201:cores=32,load1=37.9,busy=3.7,free_gb=52.5,swap=23%,mempsi=5.66,si=3880KB/s,so=89444KB/s,coauthor=false,loss=0%,"
    "cpu=71.0C,dimm=42.5C,workdir=ok,hookparse=ok,gh=ok/cached,codex=login/ok,placed=0,cap=4,slots=0,login=claude,"
    "ADMISSION-REFUSED(memory PSI 5.7% > 5%)"
)


def write(path: Path, content: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        content if isinstance(content, str) else json.dumps(content), encoding="utf-8"
    )
    return path


def test_a_reading_with_a_bracketed_pool_cell_splits_on_the_commas_outside_it() -> None:
    parts = split_reading("a=1,pool=[x host=y revoked=a,b],c=3")
    assert parts == ["a=1", "pool=[x host=y revoked=a,b]", "c=3"]


def test_a_mac_reading_yields_load_memory_cap_and_the_lane_refusal() -> None:
    reading = parse_pool_reading(H200, NOW)
    assert (reading.host, reading.cores, reading.load1, reading.busy_cores) == (
        "h200",
        24,
        19.9,
        18.0,
    )
    assert (reading.mem_free_gb, reading.mem_total_gb, reading.swap_used_pct) == (
        98.7,
        192.0,
        None,
    )
    assert (reading.cap, reading.slots) == (12, 0)
    assert reading.refusal == "CPU-IDLE-UNDER-BAR: 25.0% idle < 25%"


def test_a_linux_reading_yields_swap_and_leaves_total_memory_unread() -> None:
    reading = parse_pool_reading(H201, NOW)
    assert (reading.swap_used_pct, reading.mem_total_gb, reading.cap) == (23.0, None, 4)
    assert reading.refusal == "memory PSI 5.7% > 5%"


def test_an_unreadable_host_carries_the_runners_reason_and_no_numbers() -> None:
    reading = parse_pool_reading("h105:UNREADABLE(rc=255 timed out after 20s)", NOW)
    assert reading.error == "rc=255 timed out after 20s"
    assert (reading.load1, reading.cores, reading.cap) == (None, None, None)


def test_the_pool_receipt_is_read_at_its_start_time_and_a_missing_one_is_none(
    tmp_path: Path,
) -> None:
    path = write(
        tmp_path / "hosts-probe" / "latest.json",
        {
            "started_at": "2026-10-31T17:57:24Z",
            "readings": [H200, "h105:UNREADABLE(x)"],
        },
    )
    pool = load_pool(path)
    assert pool is not None
    assert [r.host for r in pool] == ["h200", "h105"]
    assert pool[0].read_at == datetime(2026, 10, 31, 17, 57, 24, tzinfo=UTC)
    assert load_pool(tmp_path / "absent.json") is None
    bad_path = write(tmp_path / "bad.json", "{not json")
    assert load_pool(bad_path) is None


def test_the_host_table_gives_cap_and_availability(tmp_path: Path) -> None:
    path = write(
        tmp_path / "hosts.yaml",
        yaml.safe_dump(
            {
                "hosts": [
                    {"name": "h051", "cap": 24},
                    {
                        "name": "h202",
                        "available": False,
                        "unavailable_reason": "down for repair",
                    },
                ]
            }
        ),
    )
    rows = load_host_table(path)
    assert rows is not None
    assert [(r.host, r.cap, r.available, r.unavailable_reason) for r in rows] == [
        ("h051", 24, True, ""),
        ("h202", None, False, "down for repair"),
    ]
    assert load_host_table(tmp_path / "absent.yaml") is None


def test_placement_markers_count_units_and_lanes_for_live_pids_only(
    tmp_path: Path,
) -> None:
    mine = os.getpid()
    for name in (
        f"h051.1793464348.{mine}",  # a lane (lease id is an epoch second)
        f"h051.20291.{mine}",  # a unit
        f"h101.20292.{mine}",
        "h101.20293.99999999",  # a dead pid
        "parity.json",
        ".lock",
    ):
        (tmp_path / name).write_text("x", encoding="utf-8")
    placed = load_placed(tmp_path, 1_000_000_000)
    assert placed is not None
    assert [(p.host, p.units, p.lanes) for p in placed] == [
        ("h051", 2, 1),
        ("h101", 1, 0),
    ]
    assert load_placed(tmp_path / "absent", 1_000_000_000) is None


def receipt_json(lane: str, **cells: Any) -> dict[str, Any]:
    return {
        "lane": lane,
        "run_id": f"rlane-{lane}-aaaaaa",
        "host": "h051",
        "status": "running",
        "final": False,
        "started_at": "2026-10-31T17:30:00Z",
        "model": "sonnet",
        **cells,
    }


def test_receipts_take_the_newest_file_of_each_recent_lane_directory(
    tmp_path: Path,
) -> None:
    first = write(
        tmp_path / "lane-a" / "rlane-lane-a-1.json",
        receipt_json("lane-a", started_at="2026-10-31T10:00:00Z"),
    )
    os.utime(first, (NOW.timestamp() - 3600, NOW.timestamp() - 3600))
    newest = write(tmp_path / "lane-a" / "rlane-lane-a-2.json", receipt_json("lane-a"))
    os.utime(newest, (NOW.timestamp(), NOW.timestamp()))
    write(tmp_path / "lane-a" / "rlane-lane-a-2.job.json", {"job_id": "x"})
    write(
        tmp_path / "cloud-a" / "rlane-cloud-a-1.json",
        receipt_json(
            "cloud-a",
            host="cloud:h200",
            status="cloud-no-pr",
            final=True,
            ended_at="2026-10-31T17:45:00Z",
            placement="cloud",
            cloud={
                "account": "h200",
                "session_state": {
                    "detail": "push refused (403)",
                    "needs_action": "authorize the repo",
                },
            },
            result="BLOCKER " + "x" * 900,
        ),
    )
    old = write(tmp_path / "old" / "rlane-old-1.json", receipt_json("old"))
    os.utime(old.parent, (NOW.timestamp() - 90 * 3600, NOW.timestamp() - 90 * 3600))
    receipts = load_receipts(
        tmp_path, now=NOW, window_hours=24, glob="rlane-*.json", result_chars=600
    )
    assert receipts is not None
    by_lane = {r.lane: r for r in receipts}
    assert set(by_lane) == {"lane-a", "cloud-a"}
    assert by_lane["lane-a"].started_at == datetime(2026, 10, 31, 17, 30, tzinfo=UTC)
    cloud = by_lane["cloud-a"]
    assert (
        cloud.host,
        cloud.account,
        cloud.final,
        cloud.session_detail,
        cloud.session_needs,
    ) == (
        "cloud:h200",
        "h200",
        True,
        "push refused (403)",
        "authorize the repo",
    )
    assert len(cloud.result) == 600
    assert (
        load_receipts(
            tmp_path / "absent",
            now=NOW,
            window_hours=24,
            glob="rlane-*.json",
            result_chars=600,
        )
        is None
    )


def test_cloud_accounts_read_login_cap_open_sessions_and_credit(tmp_path: Path) -> None:
    path = write(
        tmp_path / "cloud-accounts.json",
        {
            "accounts": {
                "h101": {"login": "no-claude-ai-login"},
                "h200": {
                    "login": "ok",
                    "cap": 14,
                    "active": {
                        "run-2": "2026-10-31T17:53:35Z",
                        "run-1": "2026-10-31T17:00:00Z",
                    },
                    "credit": {
                        "remaining": 244.0,
                        "read_at": "2026-10-25T18:02:35Z",
                        "source": "quoted words",
                    },
                    "rate": {
                        "status": "allowed_warning",
                        "resets_at": "2026-11-05T08:00:00Z",
                    },
                },
            }
        },
    )
    accounts = load_cloud_accounts(path)
    assert accounts is not None
    ready = accounts[1]
    assert (
        ready.name,
        ready.login,
        ready.cap,
        ready.active,
        ready.credit_remaining,
    ) == ("h200", "ok", 14, ("run-1", "run-2"), 244.0)
    assert ready.rate_status == "allowed_warning"
    assert load_cloud_accounts(tmp_path / "absent.json") is None


def test_the_runner_fleet_event_is_reduced_to_host_label_and_status(
    tmp_path: Path,
) -> None:
    path = write(
        tmp_path / "runner-fleet.json",
        {
            "observed_at": "2026-10-31T17:50:00+00:00",
            "runners": [
                {"runner_name": "r1", "host": "label-other", "status": "busy"},
                {"runner_name": "r2", "host": "label-other", "status": "offline"},
            ],
        },
    )
    fleet = load_runner_fleet(path)
    assert fleet is not None
    assert [(r.host_label, r.status) for r in fleet.runners] == [
        ("label-other", "busy"),
        ("label-other", "offline"),
    ]
    assert load_runner_fleet(tmp_path / "absent.json") is None


def test_a_dir_is_the_env_value_else_the_fallback_under_home() -> None:
    assert resolve_dir({"X": "/somewhere"}, "X", ".a/b") == Path("/somewhere")
    assert resolve_dir({}, "X", ".a/b") == Path.home() / ".a/b"


LEDGER = "\n".join(
    [
        "L1 2026-10-31T17:01:41Z | CLAIM | lane=floor-art | principal=p | ticket=OMN-21733 | actor=claude:sonnet:workflow-lane | model=claude-sonnet | est ~3 lane-hours | parent=orch-31d9 Rebuild the factory floor page | req=abc | via=bus:h200",
        "L2 2026-10-31T17:31:04Z | CLAIM | lane=fix-floor | principal=p | ticket=OMN-21733 | actor=claude:sonnet:workflow-lane | model=claude-sonnet | est ~2 lane-hours | parent=orch-31d9 | req=def | via=bus:h200 | Fix the reds on omnimarket#4679",
        "L3 2026-10-31T17:52:50Z | CLAIM | lane=landing-L20305 | principal=p | ticket=OMN-19998 | actor=script:landing-controller | model=codex_high | pr=omnibase_infra#5441 | relay=landing-controller | host=h101 | est ~1 lane-hours | req=008 | via=bus:h200 | landing fix worker for OmniNode-ai/omnibase_infra#5441",
    ]
)


def test_a_claim_row_gives_its_actor_model_host_and_sentence_whichever_way_the_sentence_was_written() -> (
    None
):
    glued, trailing, landing = parse_ledger_text(LEDGER)
    assert glued.text == "Rebuild the factory floor page"
    assert (glued.actor, glued.model, glued.host) == (
        "claude:sonnet:workflow-lane",
        "claude-sonnet",
        "",
    )
    assert trailing.text == "Fix the reds on omnimarket#4679"
    assert (landing.host, landing.model, landing.prs, landing.actor) == (
        "h101",
        "codex_high",
        ("omnibase_infra#5441",),
        "script:landing-controller",
    )


def test_the_watcher_ci_verdict_becomes_a_state_and_a_note(tmp_path: Path) -> None:
    def pr(verdict: str | None, **ci: Any) -> dict[str, Any]:
        return {
            "facts": {"state": "OPEN", "title": "t"},
            "cls": "pending",
            "ci": {"verdict": verdict, **ci},
        }

    path = write(
        tmp_path / "state.json",
        {
            "last_tick": "2026-10-31T17:59:00Z",
            "prs": {
                "r#1": pr("RED", red=["a", "b"], pending=["c"], total=152),
                "r#2": pr("GREEN"),
                "r#3": pr("PENDING", pending=["x"], total=9),
                "r#4": pr(None),
            },
        },
    )
    watcher = load_watcher(path, NOW, 90)
    assert {p.key: (p.ci, p.ci_note) for p in watcher.prs} == {
        "r#1": ("red", "2 red, 1 pending of 152"),
        "r#2": ("green", ""),
        "r#3": ("pending", "1 pending of 9"),
        "r#4": ("unread", ""),
    }


def test_titles_come_from_the_json_snapshot_and_status_types_from_the_text_file(
    tmp_path: Path,
) -> None:
    text = write(tmp_path / "linear.txt", "OMN-1 In Progress 2026-10-31T09:00:00Z\n")
    titled = write(
        tmp_path / "linear.json",
        {
            "issues": [
                {"id": "OMN-1", "title": "  The   first ", "status": "In Progress"},
                {"id": "OMN-2", "title": "Second", "status": "Done"},
            ]
        },
    )
    merged = merge_linear_titles(load_linear(text), load_linear(titled))
    assert [(i.id, i.title, i.status_type) for i in merged] == [
        ("OMN-1", "The first", "started"),
        ("OMN-2", "Second", "completed"),
    ]

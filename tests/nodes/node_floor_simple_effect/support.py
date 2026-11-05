# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders of a registry home for the 17a cycle: the floor record, the watcher state, the lab files, the overlays."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

NOW = datetime(2026, 11, 5, 15, 30, tzinfo=UTC)
OVERLAYS = Path(__file__).resolve().parent / "fixtures" / "overlays"


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def environ(home: Path) -> dict[str, str]:
    """The environment of a cycle: the overlay root, the lane directory under the home and no pinned files."""
    return {
        "ONEX_SKILL_OVERLAY_ROOTS": str(OVERLAYS),
        "ONEX_REMOTE_LANE_DIR": str(home / "lanes"),
    }


def machine(
    host: str, state: str = "read", admission: str = "open", reason: str = ""
) -> dict[str, Any]:
    return {
        "host": host,
        "state": state,
        "note": "declared by the operator" if state != "read" else "",
        "read_at": iso(NOW - timedelta(minutes=2)) if state == "read" else "",
        "admission": {
            "state": admission if state == "read" else "unread",
            "reason": reason,
        },
    }


def record(**over: Any) -> dict[str, Any]:
    floor: dict[str, Any] = {
        "machines": [machine("h051"), machine("h053"), machine("h105", "out")],
        "bays": [
            {"host": "h051", "slots": 32, "running": 8, "free": 24, "stale": False},
            {"host": "h053", "slots": 16, "running": 6, "free": 10, "stale": False},
            {"host": "h105", "slots": 3, "running": 0, "free": 0, "stale": True},
        ],
        "lanes": [
            {"lane": "landing-L1", "ticket": "OMN-19998", "stalled": False},
            {"lane": "build-a", "ticket": "OMN-19998", "stalled": False},
        ],
        "stoppages": [
            {
                "name": "lab-idle",
                "occurrences": 64,
                "cost_minutes": 20420.0,
                "recurring": True,
                "tickets": [],
                "last_seen": iso(NOW - timedelta(minutes=50)),
            },
            {
                "name": "throughput-results",
                "occurrences": 8,
                "cost_minutes": 480.0,
                "recurring": True,
                "tickets": ["OMN-19998"],
                "last_seen": iso(NOW - timedelta(hours=2)),
            },
        ],
        "merged_running": {
            "generated_at": iso(NOW - timedelta(minutes=3)),
            "days": [
                {
                    "merges": [
                        {
                            "repo": "r",
                            "number": 1,
                            "merged_at": iso(NOW - timedelta(minutes=30)),
                            "running": True,
                            "reason": "",
                        }
                    ]
                }
            ],
        },
    }
    out = {
        "schema_id": "omni.factory-floor/1",
        "generated_at": iso(NOW - timedelta(minutes=2)),
        "lines": [],
        "floor": floor,
    }
    out.update(over)
    return out


def watcher(**over: Any) -> dict[str, Any]:
    def pr(cls: str, title: str, done_minutes_ago: int | None = None) -> dict[str, Any]:
        ci = (
            {"detail": [["check", "1", iso(NOW - timedelta(minutes=done_minutes_ago))]]}
            if done_minutes_ago is not None
            else None
        )
        return {
            "cls": cls,
            "ci": ci,
            "facts": {"state": "OPEN", "draft": False, "title": title, "head_ref": "x"},
        }

    state: dict[str, Any] = {
        "last_tick": iso(NOW - timedelta(minutes=1)),
        "prs": {
            "r#1": pr("red", "fix(OMN-19998): a"),
            "r#2": pr("red", "fix(OMN-19998): b"),
            "r#3": pr("green-unarmed", "fix(OMN-22000): c", 20),
            "r#4": pr("held-excluded", "x"),
            "r#5": {"cls": "merged", "facts": {"state": "MERGED"}},
        },
        "merges": {
            "r#9": {"merged_at": iso(NOW - timedelta(minutes=10)), "title": "t"},
            "r#8": {"merged_at": iso(NOW - timedelta(minutes=90)), "title": "t"},
        },
    }
    state.update(over)
    return state


def pool_receipt(free: str = "50.0G") -> dict[str, Any]:
    entry = f"h051:cores=80,load1=4.6,disk_lanetmp={free}/97.9G/47%,disk_workdir={free}/687.9G/45%,placed=0,cap=32"
    return {"started_at": iso(NOW - timedelta(minutes=2)), "readings": [entry]}


def fleet(offline: int = 0) -> dict[str, Any]:
    return {
        "observed_at": iso(NOW - timedelta(minutes=1)),
        "runners": [],
        "class_rollup": [
            {
                "label_class": "ci",
                "total": 4,
                "online": 4 - offline,
                "busy": 1,
                "offline": offline,
            },
            {"label_class": "deploy", "total": 1, "online": 1, "busy": 0, "offline": 0},
        ],
    }


def lab_alarm_run(outcome: str = "OK", finished_minutes_ago: int = 4) -> dict[str, Any]:
    return {
        "finished_at": iso(NOW - timedelta(minutes=finished_minutes_ago)),
        "reports": [
            {
                "condition": "consumer_group_lag",
                "outcome": outcome,
                "evidence": "lag 0" if outcome == "OK" else "lag grew",
            },
            {"condition": "broker_listener", "outcome": "OK", "evidence": "answered"},
        ],
    }


def build_home(
    tmp_path: Path,
    *,
    rec: dict[str, Any] | None = None,
    watch: dict[str, Any] | None = None,
    fleet_doc: dict[str, Any] | None = None,
    pool: dict[str, Any] | None = None,
    alarm_run: dict[str, Any] | None = None,
) -> Path:
    """A registry home with every source the cycle reads. A ``None`` argument takes the healthy default."""
    home = tmp_path / "home"

    def put(rel: str, body: Any, jsonl: bool = False) -> None:
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body) + ("\n" if jsonl else ""), encoding="utf-8")

    put(
        ".claude_scratch/factory-floor/factory-floor.json",
        rec if rec is not None else record(),
    )
    put(
        ".claude_scratch/pr-watcher/state.json",
        watch if watch is not None else watcher(),
    )
    put(
        ".onex_state/runner-fleet/runner-fleet.json",
        fleet_doc if fleet_doc is not None else fleet(),
    )
    put("lanes/hosts-probe/latest.json", pool if pool is not None else pool_receipt())
    put(
        ".onex_state/lab-alarm/alarm-runs.jsonl",
        alarm_run if alarm_run is not None else lab_alarm_run(),
        jsonl=True,
    )
    return home


def overlay_text(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load(
        (OVERLAYS / node / "overlay.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded

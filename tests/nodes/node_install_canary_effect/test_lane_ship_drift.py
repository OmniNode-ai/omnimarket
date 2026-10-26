# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick reports a lab lane that ran a plugin commit other than the one the launching host had installed (OMN-17427).

Run from the skill directory: python3 -B -m pytest -q -p no:cacheprovider tests/test_lane_ship_drift.py

Operator ruling 2026-10-22T12:12:56Z: lab lanes were shipped the merged tip of main, never the installed commit, and four lab
hosts were patched by hand; "that needs to be fixed in automated". The runner now ships the installed commit; this is the
detector that keeps it so. A lane's receipt names the commit it shipped and the one the install record named at launch.
"""

from __future__ import annotations

import importlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
NODE = importlib.import_module(
    "omnimarket.nodes.node_install_canary_effect"
).NodeInstallCanaryEffect({"SHA_RE": re.compile(r"^[0-9a-f]{40}$")})

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)
INSTALLED, MAIN = "b" * 40, "a" * 40
PLUGIN = "fixture-plugin@fixture-market"


def stamp(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def claude(
    tmp_path: Path, sha: str | None = INSTALLED, *, age_min: float = 600
) -> Path:
    cdir = tmp_path / "claude"
    (cdir / "plugins").mkdir(parents=True, exist_ok=True)
    record = cdir / "plugins" / "installed_plugins.json"
    record.write_text(
        json.dumps(
            {
                "plugins": {
                    PLUGIN: [
                        {"scope": "user", **({"gitCommitSha": sha} if sha else {})}
                    ]
                }
            }
        )
    )
    at = (NOW - timedelta(minutes=age_min)).timestamp()
    os.utime(record, (at, at))
    return cdir


def receipt(
    tmp_path: Path,
    lane: str,
    *,
    shipped: str,
    installed: str | None = INSTALLED,
    minutes_ago: float = 30,
    host: str | None = "host-b",
) -> Path:
    ship = {
        "plugin": PLUGIN,
        "slug": "OmniNode-ai/repo-plugin-source",
        "sha": shipped,
        "subdir": "plugins/omni",
        "dirty": False,
        "behind": "",
    }
    if installed is not None:
        ship["installed"] = installed
    path = tmp_path / "lanes" / lane / f"rlane-{lane}-abc123.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "lane": lane,
                "host": host,
                "started_at": stamp(minutes_ago),
                "plugins": [ship],
            }
        )
    )
    at = (NOW - timedelta(minutes=minutes_ago)).timestamp()
    os.utime(path, (at, at))
    os.utime(path.parent, (at, at))
    return path


def drift(tmp_path: Path, cdir: Path, **kw):
    return NODE.lane_ship_drift(tmp_path / "lanes", cdir, NOW, **kw)


def test_a_lane_shipped_main_while_the_host_had_another_commit_installed_is_missing(
    tmp_path,
):
    """The old behaviour: the runner shipped the merged tip. This fails on it."""
    receipt(tmp_path, "lane-a", shipped=MAIN)
    found = drift(tmp_path, claude(tmp_path))
    assert [(level, loop) for level, loop, _, _ in found] == [
        ("missing", "lane-ship:lane-a")
    ]
    assert "host-b" in found[0][2]
    assert MAIN[:12] in found[0][2]
    assert INSTALLED[:12] in found[0][2]
    assert "hand-replace" in found[0][3]


def test_a_lane_shipped_the_installed_commit_is_clean(tmp_path):
    receipt(tmp_path, "lane-a", shipped=INSTALLED)
    assert drift(tmp_path, claude(tmp_path)) == []


def test_an_install_since_the_lane_launched_is_not_drift(tmp_path):
    """The record moves on every install; the lane is judged against the commit it launched under."""
    receipt(tmp_path, "lane-a", shipped=MAIN, installed=MAIN)
    assert drift(tmp_path, claude(tmp_path, INSTALLED)) == []


def test_a_receipt_without_the_recorded_commit_is_judged_only_when_it_started_after_the_record_was_written(
    tmp_path,
):
    receipt(tmp_path, "after", shipped=MAIN, installed=None, minutes_ago=30)
    receipt(tmp_path, "before", shipped=MAIN, installed=None, minutes_ago=90)
    found = drift(tmp_path, claude(tmp_path, age_min=60))
    assert [loop for _, loop, _, _ in found] == ["lane-ship:after"]


def test_a_host_with_no_recorded_commit_is_not_judged(tmp_path):
    receipt(tmp_path, "lane-a", shipped=MAIN, installed="")
    assert drift(tmp_path, claude(tmp_path, None)) == []
    assert drift(tmp_path, tmp_path / "no-claude-dir") == []


def test_a_lane_that_placed_nowhere_or_ran_long_ago_is_not_judged(tmp_path):
    receipt(tmp_path, "nowhere", shipped=MAIN, host=None)
    receipt(tmp_path, "old", shipped=MAIN, minutes_ago=7 * 60)
    assert drift(tmp_path, claude(tmp_path)) == []
    assert drift(tmp_path, claude(tmp_path), window_h=8) != []


def test_job_files_and_unreadable_receipts_are_skipped(tmp_path):
    receipt(tmp_path, "lane-a", shipped=INSTALLED)
    (tmp_path / "lanes" / "lane-a" / "rlane-lane-a-abc123.job.json").write_text("{}")
    (tmp_path / "lanes" / "lane-a" / "rlane-lane-a-bad.json").write_text("{not json")
    assert drift(tmp_path, claude(tmp_path)) == []
    assert drift(tmp_path / "missing", claude(tmp_path)) == []


def test_the_newest_lanes_are_listed_and_the_rest_counted(tmp_path):
    for i in range(7):
        receipt(tmp_path, f"lane-{i}", shipped=MAIN, minutes_ago=10 + i)
    found = drift(tmp_path, claude(tmp_path), limit=3)
    assert [loop for _, loop, _, _ in found] == [
        "lane-ship:lane-0",
        "lane-ship:lane-1",
        "lane-ship:lane-2",
    ]
    assert "and 4 more" in found[-1][2]


def test_the_tick_prints_the_finding(tmp_path, monkeypatch):
    receipt(tmp_path, "lane-a", shipped=MAIN)
    cdir = claude(tmp_path)
    found = NODE.tick_findings(
        NOW,
        {
            "HOME": str(tmp_path),
            "CLAUDE_CONFIG_DIR": str(cdir),
            "ONEX_REMOTE_LANE_DIR": str(tmp_path / "lanes"),
        },
    )
    assert [(level, loop) for level, loop, _, _ in found] == [
        ("missing", "lane-ship:lane-a")
    ]


def test_an_unreadable_receipt_directory_is_one_unknown_and_the_tick_still_prints(
    tmp_path,
):
    class Broken(type(NODE.handler)):
        def lane_ship_drift(self, *args, **kwargs):
            raise RuntimeError("receipts\nunreadable")

    node = type(NODE)({"SHA_RE": re.compile(r"^[0-9a-f]{40}$")})
    node.handler = Broken({"SHA_RE": re.compile(r"^[0-9a-f]{40}$")})
    found = node.tick_findings(NOW, {"HOME": str(tmp_path)})
    assert [(level, loop, why) for level, loop, why, _ in found] == [
        ("unknown", "lane-ship", "receipts unreadable")
    ]

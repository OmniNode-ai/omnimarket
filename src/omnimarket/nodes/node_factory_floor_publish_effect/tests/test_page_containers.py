# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pages draw application containers per lane class from the line's record, and name why a lane has no drawn machine (OMN-21930)."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

PAGES = Path(__file__).resolve().parents[1] / "page"

HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesJson] = process.argv.slice(1);
    const files = JSON.parse(filesJson);
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(fs.readFileSync(pagePath, "utf8"))[1];
    const els = {};
    const mk = id => ({id, innerHTML: "", textContent: "", hidden: false, dataset: {}, style: {}, className: "",
      tabIndex: 0, title: "", children: [], classList: {add() {}, remove() {}},
      querySelectorAll: () => [], setAttribute() {}, addEventListener() {}, replaceChildren() {},
      appendChild(c) { this.children.push(c); }, focus() {}});
    globalThis.document = {title: "", getElementById: id => els[id] || (els[id] = mk(id)), addEventListener() {},
      querySelectorAll: () => [], createElement: () => mk("created")};
    globalThis.window = {addEventListener() {}};
    globalThis.location = {hash: "", search: ""};
    globalThis.history = {replaceState() {}};
    globalThis.localStorage = {getItem: () => null, setItem() {}};
    globalThis.getComputedStyle = () => ({getPropertyValue: () => "1"});
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => {
      const key = Object.keys(files).find(k => url.endsWith(k));
      if (!key) return {ok: false, status: 404, json: async () => ({})};
      return {ok: true, status: 200, json: async () => JSON.parse(files[key])};
    };
    (async () => {
      new Function(script).call(globalThis);
      await new Promise(r => setTimeout(r, 150));
      const out = {};
      for (const [k, v] of Object.entries(els)) out[k] = v.innerHTML + "|" + v.textContent;
      console.log(JSON.stringify(out));
    })();
    """
)


def _draw(page: str, record: dict[str, Any]) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    files = {
        "factory-floor.json": record,
        "publish-status.json": {"published_at": record["generated_at"], "steps": []},
    }
    done = subprocess.run(
        [
            node,
            "-e",
            HARNESS,
            str(PAGES / page),
            json.dumps({k: json.dumps(v) for k, v in files.items()}),
        ],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    out: dict[str, str] = json.loads(done.stdout.decode().strip().splitlines()[-1])
    return out


def _container(name: str, **cells: Any) -> dict[str, Any]:
    return {
        "host": "h201",
        "lane": name,
        "status": "PASS",
        "observed": 1,
        "budget": 1,
        "alarm_key": "",
        "detail": "1 application containers running, budget 1",
        **cells,
    }


# The line that carries the container read in the live record read at 2026-11-02T01:21:53Z.
CONTAINERS = {
    "state": "read",
    "lanes": [
        _container("collaborator", observed=6, budget=6),
        _container("dev", observed=19, budget=19),
        _container("stability", observed=11, budget=11),
    ],
}


def _lane(name: str, **cells: Any) -> dict[str, Any]:
    return {
        "lane": name,
        "ticket": "OMN-9",
        "title": f"{name} work",
        "title_source": "claim",
        "kind": "build",
        "engine": "claude",
        "placement": "machine",
        "host": "m1",
        "placement_cause": "",
        "named_host": "",
        "cloud_account": "",
        "started_at": "2099-01-01T00:00:00Z",
        "run_minutes": 5,
        "status_text": "working",
        "status_full": "working",
        "status_at": "2099-01-01T00:00:00Z",
        "status_age_min": 1,
        "status_source": "status",
        "silent_minutes": 1,
        "stalled": False,
        "pr": "",
        "pr_title": "",
        "pr_state": "",
        "pr_ci": "",
        "pr_ci_note": "",
        "station": None,
        "claim_full": "claim",
        **cells,
    }


def _record(
    containers: Any = None, lanes: tuple[dict[str, Any], ...] = ()
) -> dict[str, Any]:
    line: dict[str, Any] = {
        "line_id": "plan-one",
        "name": "First plan",
        "plan_path": "beta/plans/one-plan.md",
        "source": "manifest",
        "status_pill": "building",
        "percent_built": 8,
        "andon": {"flag": False, "reason": "", "causes": []},
        "stations": [
            {
                "id": "0",
                "name": "Look first",
                "state": "active",
                "feeders": [],
                "items": [
                    {
                        "id": "a",
                        "title": "Part A",
                        "kind": "lane",
                        "state": "active",
                        "note": "",
                        "tickets": [],
                        "prs": [],
                    }
                ],
            }
        ],
    }
    if containers is not None:
        line["application_containers"] = containers
    return {
        "schema_id": "factory-floor/1",
        "generated_at": "2099-01-01T00:00:00Z",
        "lines": [line],
        "floor": {
            "machines": [],
            "cloud": {
                "state": "read",
                "note": "",
                "accounts": [],
                "sessions": [],
                "lamps": [],
            },
            "lanes": list(lanes),
            "stall_minutes": 45,
            "lane_window_minutes": 180,
            "bays": [],
            "shipped": [],
            "waiting": [],
            "stoppages": [],
        },
        "sources": {"watcher_last_tick": "2099-01-01T00:00:00Z", "ledger_rows": 1},
    }


def test_containers_floor_page_draws_running_of_declared_per_lane_class() -> None:
    lines = _draw("floor/index.html", _record(CONTAINERS))["lines"]
    assert "Application containers running of declared" in lines
    for drawn in ("dev 19 of 19", "stability 11 of 11", "collaborator 6 of 6"):
        assert drawn in lines


def test_containers_dashboard_card_draws_the_same_figures() -> None:
    dash = _draw("index.html", _record(CONTAINERS))["dash"]
    for drawn in ("dev 19 of 19", "stability 11 of 11", "collaborator 6 of 6"):
        assert drawn in dash


def test_containers_a_record_with_none_draws_no_row() -> None:
    assert "plan-one" in _draw("floor/index.html", _record())["lines"]
    for record in (_record(), _record(None)):
        assert (
            "Application containers" not in _draw("floor/index.html", record)["lines"]
        )
        assert "Application containers" not in _draw("index.html", record)["dash"]
    explicit_null = _record()
    explicit_null["lines"][0]["application_containers"] = None
    assert (
        "Application containers"
        not in _draw("floor/index.html", explicit_null)["lines"]
    )
    assert "Application containers" not in _draw("index.html", explicit_null)["dash"]


def test_containers_an_unread_host_is_drawn_unread_and_an_off_budget_lane_is_marked() -> (
    None
):
    mixed = {
        "state": "read",
        "lanes": [
            _container("dev", observed=21, budget=19, status="FAIL"),
            _container("stability", observed=None, budget=11, status="UNKNOWN"),
        ],
    }
    lines = _draw("floor/index.html", _record(mixed))["lines"]
    assert "dev 21 of 19 (off budget)" in lines
    assert "stability unread" in lines
    assert "0 of 11" not in lines
    nothing = {"state": "unread", "lanes": []}
    assert (
        "unread, no host could be read"
        in _draw("floor/index.html", _record(nothing))["lines"]
    )


def test_containers_every_character_of_the_record_is_escaped() -> None:
    hostile = {
        "state": "read",
        "lanes": [_container("<img src=x>", detail="<script>1</script>")],
    }
    for page, key in (("floor/index.html", "lines"), ("index.html", "dash")):
        drawn = _draw(page, _record(hostile))[key]
        assert "<img src=x>" not in drawn
        assert "<script>1</script>" not in drawn


def _unplaced() -> tuple[dict[str, Any], ...]:
    def lane(name: str, cause: str, named: str = "") -> dict[str, Any]:
        return _lane(
            name, host="", placement="unplaced", placement_cause=cause, named_host=named
        )

    return (
        lane("landing-cause-L1", "no-host-cell"),
        lane("dispatcher-1", "no-host-cell"),
        lane("on-the-201", "host-not-drawn", "h201-dev"),
    )


def test_containers_unplaced_lanes_are_counted_by_cause_with_their_names() -> None:
    bays = _draw("floor/index.html", _record(lanes=_unplaced()))["bayGrid"]
    assert "2 carry no host cell" in bays
    assert "landing-cause-L1, dispatcher-1" in bays
    assert "1 name a host the floor does not draw (h201-dev)" in bays
    assert "on-the-201" in bays


def test_containers_dashboard_unplaced_summary_states_the_lanes_it_counts() -> None:
    dash = _draw("index.html", _record(lanes=_unplaced()))["dash"]
    assert (
        "3 live lanes name no machine the floor draws: 2 carry no host cell; "
        "1 name a host the floor does not draw (h201-dev)"
    ) in dash
    assert "landing-cause-L1, dispatcher-1" in dash


def test_containers_a_record_without_causes_says_so_and_does_not_invent_one() -> None:
    old = _lane("old-record-lane", host="", placement="unplaced")
    del old["placement_cause"], old["named_host"]
    dash = _draw("index.html", _record(lanes=(old,)))["dash"]
    assert "written before causes were recorded" in dash
    assert copy.deepcopy(old)["lane"] == "old-record-lane"

# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The page draws each machine's free slots from the record's `free_slots` and `admission`, never from `cap - lanes` (OMN-21733)."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from typing import Any, cast

import pytest

from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    BAYS_RECORD,
    NODE_DIR,
    _run_floor,
    machine_cells,
)

PAGE = (NODE_DIR / "page/floor/index.html").read_text(encoding="utf-8")
REASON = "CPU-IDLE-UNDER-BAR: 15.4% idle < 25%; RESERVE-MEMORY-FLOOR: 12.3G available"

FACTS_HARNESS = """
const fs = require("fs");
const [pagePath, floorJson] = process.argv.slice(1);
const script = /<script>([\\s\\S]*?)<\\/script>/.exec(fs.readFileSync(pagePath, "utf8"))[1];
const grab = name => new RegExp("function " + name + "\\\\([^)]*\\\\)\\\\{[\\\\s\\\\S]*?\\\\n\\\\}\\\\n").exec(script)[0];
const num = (v, d = 0) => Number.isFinite(Number(v)) ? Number(v) : d;
const isRefused = new Function(grab("isRefused") + "; return isRefused;")();
const facts = new Function("num", "isRefused", grab("machineFacts") + "; return machineFacts;")(num, isRefused);
console.log(JSON.stringify(facts(JSON.parse(floorJson))));
"""


def bays(*machines: dict[str, object]) -> dict[str, Any]:
    record: dict[str, Any] = copy.deepcopy(BAYS_RECORD)
    record["floor"]["machines"] = list(machines)
    return record


def bay_html(record: dict[str, Any], host: str) -> str:
    grid = _run_floor({"factory-floor.json": record})["bayGrid"]
    start = grid.index(f'id="bay-{host}"')
    end = grid.find("</article>", start)
    return grid[start:end]


def test_page_draws_the_records_free_slots_not_cap_minus_lanes() -> None:
    open_bay = machine_cells(
        cap=12,
        lanes=["lane-one"],
        lanes_placed=1,
        free_slots=3,
        admission={"state": "open", "reason": ""},
    )
    html = bay_html(bays(open_bay), "bay-x")
    assert html.count("<i></i>") == 3  # the record's 3, not 12 - 1 lanes
    assert "1 running, 0 stalled, 3 free" in html


def test_page_draws_refused_and_the_reason_in_place_of_free_squares() -> None:
    refused = machine_cells(
        cap=12,
        lanes=["lane-one"],
        lanes_placed=1,
        refusal=REASON,
        free_slots=0,
        admission={"state": "refused", "reason": REASON},
    )
    html = bay_html(bays(refused), "bay-x")
    assert "refused: " + REASON.split(";")[0].replace("<", "&lt;") in html
    assert "<i></i>" not in html
    assert "not taking new work" not in html
    assert "free" not in re.sub(r"<[^>]+>", " ", html.split("capline")[1]).split("·")[0]


def test_page_machine_facts_sum_only_the_open_machines() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    floor = {
        "machines": [
            machine_cells(host="a", cap=12, free_slots=7),
            machine_cells(
                host="b",
                cap=12,
                free_slots=0,
                admission={"state": "refused", "reason": REASON},
            ),
            machine_cells(host="c", cap=4, free_slots=2),
            machine_cells(
                host="d",
                state="unread",
                cap=9,
                free_slots=0,
                admission={"state": "unread", "reason": ""},
            ),
        ],
        "lanes": [],
    }
    done = subprocess.run(
        [
            node,
            "-e",
            FACTS_HARNESS,
            str(NODE_DIR / "page/floor/index.html"),
            json.dumps(floor),
        ],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    facts = cast(
        "dict[str, Any]", json.loads(done.stdout.decode().strip().splitlines()[-1])
    )
    assert facts["cap"] == 16  # a + c, the open ones; b is refused, d is unread
    assert facts["free"] == 9  # 7 + 2
    assert facts["refused"] == 1

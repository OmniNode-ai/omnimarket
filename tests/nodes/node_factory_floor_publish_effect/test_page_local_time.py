# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pages print every time in the viewer's zone, judge today by the viewer's day, and age minutes against the real clock."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import textwrap
from typing import Any, cast

import pytest

from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    FLOOR_RECORD,
    NODE_DIR,
    SHELL_FILES,
)

CLOCK_HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, filesJson, nowIso] = process.argv.slice(1);
    const files = JSON.parse(filesJson);
    const fixed = Date.parse(nowIso);
    const RealDate = Date;
    globalThis.Date = class extends RealDate {
      constructor(...a) { if (a.length) super(...a); else super(fixed); }
      static now() { return fixed; }
    };
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

NOW_NEW_YORK = "2026-11-01T02:04:00Z"  # 10:04 pm on Oct 31 in New York


def _run_at(page: str, files: dict[str, Any], now: str, tz: str) -> dict[str, str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [
            node,
            "-e",
            CLOCK_HARNESS,
            str(NODE_DIR / "page" / page),
            json.dumps({k: json.dumps(v) for k, v in files.items()}),
            now,
        ],
        capture_output=True,
        check=False,
        timeout=60,
        env={**os.environ, "TZ": tz},
    )
    assert done.returncode == 0, done.stderr.decode()
    out = cast(
        "dict[str, str]", json.loads(done.stdout.decode().strip().splitlines()[-1])
    )
    return {k: v.replace(chr(0x202F), " ") for k, v in out.items()}


def _files(generated: str, series_day: str, window_end: str) -> dict[str, Any]:
    files = copy.deepcopy(SHELL_FILES)
    files["factory-floor.json"]["generated_at"] = generated
    day = files["process-health.json"]["days"][-1]
    day["date"] = series_day
    day["window"]["end"] = window_end
    return files


def test_page_time_new_york_dashboard_shows_local_time_with_zone_and_no_not_today_warning() -> (
    None
):
    files = _files(NOW_NEW_YORK, "2026-10-31", "2026-11-01T02:00:00Z")
    out = _run_at("index.html", files, NOW_NEW_YORK, "America/New_York")
    assert "Record computed Oct 31, 10:04 PM EDT" in out["stamp"]
    assert "UTC" not in out["stamp"]
    assert "Series day 2026-10-31, so far (to Oct 31, 10:00 PM EDT)" in out["dash"]
    assert "not today" not in out["dash"]
    assert "UTC" not in out["dash"]


def test_page_time_new_york_still_warns_when_the_series_is_a_local_day_behind() -> None:
    files = _files(NOW_NEW_YORK, "2026-10-30", "2026-10-31T02:00:00Z")
    out = _run_at("index.html", files, NOW_NEW_YORK, "America/New_York")
    assert "This is not today (2026-10-31): the series is behind." in out["dash"]


def test_page_time_utc_viewer_sees_utc_and_the_utc_day() -> None:
    files = _files(NOW_NEW_YORK, "2026-10-31", "2026-11-01T02:00:00Z")
    out = _run_at("index.html", files, NOW_NEW_YORK, "UTC")
    assert "Record computed Nov 1, 2:04 AM UTC" in out["stamp"]
    assert "This is not today (2026-11-01): the series is behind." in out["dash"]


def test_page_time_new_york_floor_page_shows_local_time_with_zone() -> None:
    record = copy.deepcopy(FLOOR_RECORD)
    record["generated_at"] = NOW_NEW_YORK
    out = _run_at(
        "floor/index.html",
        {"factory-floor.json": record},
        NOW_NEW_YORK,
        "America/New_York",
    )
    assert "Record computed Oct 31, 10:04 PM EDT" in out["stamp"]
    assert "UTC" not in out["stamp"]


def test_page_time_ages_in_the_record_count_from_the_real_clock_not_the_record_time() -> (
    None
):
    # The record was computed at 02:04Z with a machine read "0 min" old and a lane silent 60 min;
    # four hours later the page must say 4.0 h and 5.0 h, not "under a minute" and "60 min".
    files = _files(NOW_NEW_YORK, "2026-11-01", "2026-11-01T02:00:00Z")
    machine = files["factory-floor.json"]["floor"]["machines"][0]
    machine.pop("read_at")
    out = _run_at("index.html", files, "2026-11-01T06:04:00Z", "UTC")
    assert "4.0 h ago" in out["dash"]
    assert "stalled 5.0 h" in out["dash"]

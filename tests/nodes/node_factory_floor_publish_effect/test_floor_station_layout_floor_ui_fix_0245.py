# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor page in a real browser: no two stations collide, and a station's heading is plain words.

The record carries a 27-station line written from the recorded Line A of the validators plan: its stations are the
plan's task lines, with their numbers, tickets and falsifier sentences, and some of them are being built beside staged
ones. The page is served over http from a temporary directory, drawn by a headless Chrome at three widths, and the
test reads back the bounding box of every station and of everything inside it. It fails when a box inside one station
overlaps a box inside another, when a station's content spills outside its own box, when the line card scrolls
sideways, or when a heading shows a task number, a ticket or falsifier text.

The test needs a Chrome or Chromium binary (CHROME_BIN, the PATH, the macOS application, or a Playwright download).
"""

from __future__ import annotations

import functools
import glob
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    NODE_DIR,
)

FIXTURE = json.loads(
    (
        NODE_DIR.parents[3]
        / "tests/nodes/node_factory_floor_publish_effect/floor_p0_record.json"
    ).read_text(encoding="utf-8")
)
WIDTHS = (1728, 1280, 390)

# The station names as the record carried them on 2026-11-04 for the validators line: the plan's own task sentences.
RECORDED_TASKS = (
    (
        "T0.1",
        "T0.1 Private vocabulary resolution for every core validator (UPDATE)",
        "prep",
    ),
    (
        "T0.2",
        "T0.2 + K11 (OMN-21495). -- falsifier: the D.2 K11 fixtures; the equality assertion of omni",
        "active",
    ),
    (
        "T1.1",
        "T1.1 + K2 (OMN-21515). Add handler_outside_node and cli_entry_outside_node to the canonica",
        "active",
    ),
    (
        "T1.2",
        "T1.2 Apply it everywhere, including the frozen repositories (UPDATE)",
        "prep",
    ),
    ("T2.1", "T2.1 Wire the core validators each repository lacks (REUSE)", "prep"),
    ("T2.2", "T2.2 Delete the 12 copies and consume core (UPDATE)", "prep"),
    (
        "T2.3",
        "T2.3 Cross-repo duplicate scripts into core check nodes (REUSE, then a very long tail of words)",
        "prep",
    ),
)

COLLECT = """
(function () {
  function collect() {
    var stations = Array.prototype.slice.call(document.querySelectorAll(".st"));
    if (!stations.length) return false;
    var boxes = stations.map(function (st) {
      var own = st.getBoundingClientRect();
      var inner = Array.prototype.slice.call(st.querySelectorAll("*")).map(function (e) {
        var r = e.getBoundingClientRect();
        return {tag: e.tagName, cls: String(e.className), l: r.left, t: r.top, r: r.right, b: r.bottom, text: (e.textContent || "").slice(0, 30)};
      }).filter(function (x) { return x.r - x.l > 1 && x.b - x.t > 1; });
      return {own: {l: own.left, t: own.top, r: own.right, b: own.bottom}, inner: inner};
    });
    var overlaps = [], spills = [];
    function hit(a, b) { return a.l < b.r - 1 && b.l < a.r - 1 && a.t < b.b - 1 && b.t < a.b - 1; }
    for (var i = 0; i < boxes.length; i++) {
      var A = boxes[i];
      for (var j = i + 1; j < boxes.length; j++) {
        var B = boxes[j];
        if (hit(A.own, B.own)) overlaps.push([i, j, "station"]);
        for (var x = 0; x < A.inner.length; x++) for (var y = 0; y < B.inner.length; y++)
          if (hit(A.inner[x], B.inner[y])) overlaps.push([i, j, A.inner[x].cls + " / " + B.inner[y].cls]);
      }
      A.inner.forEach(function (e) {
        if (e.r > A.own.r + 1 || e.l < A.own.l - 1) spills.push([i, e.cls, e.text]);
      });
    }
    var scroll = document.querySelector(".floor-scroll");
    var out = {
      width: window.innerWidth, stations: stations.length, overlaps: overlaps.slice(0, 5), spills: spills.slice(0, 5),
      sideways: scroll ? scroll.scrollWidth > scroll.clientWidth + 1 : false,
      docSideways: document.documentElement.scrollWidth > window.innerWidth + 1,
      headings: Array.prototype.slice.call(document.querySelectorAll(".st h3")).map(function (h) { return h.textContent; }),
      text: Array.prototype.slice.call(document.querySelectorAll(".st")).map(function (s) { return s.textContent; }).join("\\n")
    };
    var pre = document.createElement("pre");
    pre.id = "station-layout-result";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
    return true;
  }
  var tries = 0;
  var timer = setInterval(function () { if (collect() || ++tries > 200) clearInterval(timer); }, 50);
})();
"""


def chrome_binary() -> str | None:
    candidates = [os.environ.get("CHROME_BIN", "")]
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome-headless-shell",
    ):
        candidates.append(shutil.which(name) or "")
    candidates.append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    for pattern in (
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell",
        "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux*/headless_shell",
    ):
        candidates.extend(sorted(glob.glob(os.path.expanduser(pattern))))
    return next((c for c in candidates if c and os.access(c, os.X_OK)), None)


def line_with_stations(
    stations: tuple[tuple[str, str, str], ...], copies: int
) -> dict[str, Any]:
    line = json.loads(json.dumps(FIXTURE["lines"][0]))
    line["line_id"] = "line-a"
    line["name"] = "Validators to core, hard-coded rules out of code: plan (OMN-21433)"
    line["condition"] = "stopped"
    line["blocker"] = {
        "reason": "PR omnimemory#713 CI red (1 red of 48) (+3 more)",
        "evidence": ["omnimemory#713", "T0.2"],
    }
    built: list[dict[str, Any]] = []
    for n in range(copies):
        for task_id, name, state in stations:
            sid = f"{task_id}-{n}"
            built.append(
                {
                    "id": sid,
                    "name": name,
                    "state": state,
                    "feeders": [],
                    "items": [
                        {
                            "id": sid,
                            "title": name,
                            "kind": "lane",
                            "state": state,
                            "evidence_kind": "",
                            "note": "PR open, red: none,watcher-open-pr"
                            if state == "active"
                            else "not started: no-ticket",
                            "tickets": [],
                            "prs": ["omnimemory#713", "omnibase_core#2111"]
                            if state == "active"
                            else [],
                        }
                    ],
                }
            )
    line["stations"] = built
    return line


@pytest.fixture(scope="module")
def served_site(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, int]]:
    site = tmp_path_factory.mktemp("site")
    (site / "floor").mkdir()
    page = (NODE_DIR / "page/floor/index.html").read_text(encoding="utf-8")
    (site / "floor" / "index.html").write_text(
        page.replace("</body>", f"<script>{COLLECT}</script></body>"), encoding="utf-8"
    )
    record = json.loads(json.dumps(FIXTURE))
    record["lines"] = [line_with_stations(RECORDED_TASKS, 4)]
    (site / "factory-floor.json").write_text(json.dumps(record), encoding="utf-8")
    (site / "publish-status.json").write_text(
        json.dumps({"published_at": "2099-01-01T00:01:00Z", "steps": []}),
        encoding="utf-8",
    )

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            return None

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=str(site))
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield site, server.server_address[1]
    finally:
        server.shutdown()


def draw(port: int, width: int, tmp: Path) -> dict[str, Any]:
    binary = chrome_binary()
    if binary is None:
        pytest.skip("no Chrome or Chromium binary is installed")
    result = subprocess.run(
        [
            binary,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            f"--user-data-dir={tmp}",
            f"--window-size={width},1000",
            "--virtual-time-budget=8000",
            "--dump-dom",
            f"http://127.0.0.1:{port}/floor/?embed=1",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    found = re.search(
        r'<pre id="station-layout-result">(.*?)</pre>', result.stdout, re.DOTALL
    )
    assert found, f"the page drew no stations at {width}px: {result.stderr[-400:]}"
    text = (
        found.group(1)
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )
    out: dict[str, Any] = json.loads(text)
    return out


@pytest.mark.parametrize("width", WIDTHS)
def test_no_two_stations_overlap_and_no_station_spills(
    served_site: tuple[Path, int], tmp_path: Path, width: int
) -> None:
    _, port = served_site
    out = draw(port, width, tmp_path)
    assert out["stations"] == len(RECORDED_TASKS) * 4
    assert out["overlaps"] == [], (
        f"station boxes overlap at {width}px: {out['overlaps']}"
    )
    assert out["spills"] == [], f"station content spills at {width}px: {out['spills']}"
    assert not out["sideways"], f"the line card scrolls sideways at {width}px"
    assert not out["docSideways"], f"the page is wider than the screen at {width}px"


def test_station_headings_are_plain_words_without_task_numbers_or_falsifiers(
    served_site: tuple[Path, int], tmp_path: Path
) -> None:
    _, port = served_site
    out = draw(port, WIDTHS[1], tmp_path)
    for heading in out["headings"]:
        assert heading.strip(), heading
        assert not re.search(r"falsifier|OMN-\d+|\bK\d+\b|_", heading, re.IGNORECASE), (
            heading
        )
        assert not re.match(r"^T\d", heading), heading
        assert len(heading.split()) <= 9, heading
    assert not re.search(r"falsifier|D\.2 K11", out["text"], re.IGNORECASE)
    assert (
        "Task T0.2-0" in out["headings"]
    )  # a task with no words of its own is named by its number
    assert "Add handler outside node and cli entry outside…" in out["headings"]

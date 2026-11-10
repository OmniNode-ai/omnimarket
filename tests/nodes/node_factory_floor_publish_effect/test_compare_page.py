# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 and AC3 of OMN-23018: the compare page is served by the floor's publisher and draws the better and worse lines.

The chain: the simple floor on the registry home, the comparison node's cycle, the publisher's copy to the served
directory, an HTTP read of ``compare.html`` and ``floor-compare.json``, and the page's own script under node drawing the
served comparison: one card per category, the details disclosure closed until opened, then the search, the category filter,
the sort and the paging of the details table, clicked through the page's own handlers. The failing controls are a
comparison with a planted gap, which the page draws as unread and never as better or worse, and a details list of 60
numbers, which the page draws 25 to a page.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from functools import partial
from http.client import HTTPConnection
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from threading import Thread

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_factory_floor_publish_effect.handlers.handler_factory_floor_publish import (
    HandlerFactoryFloorPublish,
    load_runtime,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_factory_floor_publish import (
    ModelFactoryFloorPublishRequest,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    HandlerFloorCompare,
    build_comparison,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelFloorCompareRequest,
)
from tests.nodes.node_factory_floor_publish_effect.test_factory_floor_publish import (
    LocalSsh,
    _home,
    _overlay,
)
from tests.nodes.node_floor_compare_effect.support import (
    NOW,
    OVERLAY,
    RT,
    seed,
    served_floor,
    snap,
)
from tests.nodes.node_floor_compare_effect.test_categories import (
    PLANTED_NOW,
    PLANTED_THEN,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_factory_floor_publish_effect"
)
COMPARE_SRC = (NODE_DIR / "page/compare.html").read_text(encoding="utf-8")
SIMPLE_SRC = (NODE_DIR / "page/simple.html").read_text(encoding="utf-8")


def _compare(home: Path, history: tuple) -> None:
    simple = home / RT.simple_relpath
    simple.parent.mkdir(parents=True, exist_ok=True)
    simple.write_text(json.dumps(served_floor()), encoding="utf-8")
    seed(home, *history)
    result = HandlerFloorCompare(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        ModelFloorCompareRequest(home=str(home), now=NOW, overlay=OVERLAY)
    )
    assert result.ok


def _publish_and_read(tmp_path: Path, names: tuple[str, ...]) -> dict[str, bytes]:
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    with HTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(site))
    ) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            HandlerFactoryFloorPublish(LocalSsh()).handle(
                ModelFactoryFloorPublishRequest(
                    home=str(tmp_path / "home"),
                    overlay=_overlay(tmp_path),
                    skip_refresh=True,
                    now=NOW,
                ),
                environ={},
            )
            served: dict[str, bytes] = {}
            for name in names:
                conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
                conn.request("GET", f"/{name}")
                response = conn.getresponse()
                served[name] = response.read() if response.status == 200 else b""
                conn.close()
            return served
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_the_publisher_serves_the_compare_page_and_the_comparison(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    _compare(home, (snap(1, {"open_prs": 12, "merges": 4}), snap(7, {"open_prs": 20})))
    served = _publish_and_read(
        tmp_path, ("compare.html", "floor-compare.json", "publish-status.json")
    )
    assert served["compare.html"] == (NODE_DIR / "page/compare.html").read_bytes()
    doc = json.loads(served["floor-compare.json"])
    assert doc["schema_id"] == "omni.floor-compare/1"
    assert [w["days"] for w in doc["windows"]] == [1, 7, 30]
    day = doc["windows"][0]
    line = next(x for x in day["better"] if x["key"] == "open_prs")
    assert (line["old"], line["new"], line["better_is"]) == (
        12.0,
        8.0,
        "lower_is_better",
    )
    assert json.loads(served["publish-status.json"])["floor_compare"] == "published"


def test_error_no_comparison_file_means_no_data_is_published_and_the_status_says_absent(
    tmp_path: Path,
) -> None:
    _home(tmp_path)
    served = _publish_and_read(
        tmp_path, ("compare.html", "floor-compare.json", "publish-status.json")
    )
    assert served["compare.html"]  # the page is served, and draws its own failure
    assert served["floor-compare.json"] == b""
    assert json.loads(served["publish-status.json"])["floor_compare"] == "absent"


def test_the_site_root_carries_a_link_to_the_compare_page() -> None:
    assert 'href="compare.html"' in SIMPLE_SRC
    assert load_runtime().names.compare_page == "compare.html"
    assert load_runtime().names.compare_data == "floor-compare.json"


def test_the_page_reads_the_served_comparison_and_has_the_three_way_switch() -> None:
    assert 'DATA = "floor-compare.json"' in COMPARE_SRC
    assert "data-days" in COMPARE_SRC
    assert "aria-pressed" in COMPARE_SRC
    for needed in (
        "old_text",
        "new_text",
        "change_text",
        "details_page_size",
        "categories_error",
        "biggest_change",
    ):
        assert needed in COMPARE_SRC, needed


def test_the_details_are_a_disclosure_that_starts_closed() -> None:
    assert '<details id="details" class="details">' in COMPARE_SRC
    assert '<details id="details" class="details" open' not in COMPARE_SRC


HARNESS = textwrap.dedent(
    """
    const fs = require("fs");
    const [pagePath, hash, nowIso] = process.argv.slice(1);
    // The comparison file and the steps arrive on stdin: a large comparison is longer than one argument may be on Linux.
    const payload = JSON.parse(require('fs').readFileSync(0, 'utf8'));
    const files = payload.files;
    const steps = payload.steps;
    const src = fs.readFileSync(pagePath, "utf8");
    const script = /<script>([\\s\\S]*?)<\\/script>/.exec(src)[1];
    const els = {};
    const mk = id => ({id, innerHTML: "", textContent: "", value: "", open: false, h: {},
      addEventListener(t, f) { (this.h[t] = this.h[t] || []).push(f); }, scrollIntoView() {}});
    globalThis.document = {title: "", getElementById: id => els[id] || (els[id] = mk(id))};
    for (const id of ["alert", "sw", "intro", "cards", "dbody", "dsum", "cat", "q", "ver", "sort", "details"]) document.getElementById(id);
    const win = {h: {}, addEventListener(t, f) { (this.h[t] = this.h[t] || []).push(f); }};
    globalThis.window = win;
    let current = hash;
    globalThis.location = {get hash() { return current; }, set hash(v) { current = "#" + v; (win.h.hashchange || []).forEach(f => f()); }};
    const now = Date.parse(nowIso);
    Date.now = () => now;
    globalThis.setInterval = () => 0;
    globalThis.fetch = async url => (url in files
      ? {ok: true, status: 200, json: async () => JSON.parse(files[url])}
      : {ok: false, status: 404, json: async () => ({})});
    const grab = () => ({alert: els.alert.innerHTML, sw: els.sw.innerHTML, intro: els.intro.textContent, cards: els.cards.innerHTML,
      dbody: els.dbody.innerHTML, dsum: els.dsum.textContent, cat: els.cat.innerHTML, catValue: els.cat.value,
      detailsOpen: els.details.open, title: document.title});
    (async () => {
      new Function(script)();
      await new Promise(r => setTimeout(r, 50));
      const out = [grab()];
      for (const s of steps) {
        const el = els[s.on];
        if (s.value !== undefined) el.value = s.value;
        for (const f of el.h[s.type] || []) f({target: s.dataset ? {closest: () => ({dataset: s.dataset})} : el});
        out.push(grab());
      }
      process.stdout.write(JSON.stringify(out));
    })();
    """
)


def _render_steps(doc: bytes | None, hash_: str, steps: list[dict]) -> list[dict]:
    node = shutil.which("node")
    assert node, "the compare page render test requires Node.js"
    files = {} if doc is None else {"floor-compare.json": doc.decode()}
    out = subprocess.run(
        [
            node,
            "-e",
            HARNESS,
            str(NODE_DIR / "page/compare.html"),
            hash_,
            NOW.isoformat(),
        ],
        input=json.dumps({"files": files, "steps": steps}),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout)


def _render(doc: bytes | None, hash_: str) -> dict:
    return _render_steps(doc, hash_, [])[0]


def _doc(comparison) -> bytes:
    return comparison.model_dump_json().encode("utf-8")


def _rows(html: str) -> int:
    return html.count('<tr><td class="nm">')


def _many_doc(n: int, *, overlay=OVERLAY) -> bytes:
    then = {f"git_merges:repo{i:03d}": 10.0 for i in range(n)}
    now = {f"git_merges:repo{i:03d}": 10.0 + (i % 7) - 3 for i in range(n)}
    return _doc(build_comparison((snap(1, then),), snap(0, now), RT, overlay))


@pytest.mark.parametrize(
    ("hash_", "label", "better"),
    [("", "1 day", 1), ("#7", "7 days", 2), ("#30", "30 days", 0)],
)
def test_the_switch_draws_one_card_per_category_for_each_window(
    tmp_path: Path, hash_: str, label: str, better: int
) -> None:
    home = _home(tmp_path)
    _compare(
        home,
        (
            snap(1, {"open_prs": 12, "merges": 4}),
            snap(7, {"open_prs": 20, "merges": 1}),
        ),
    )
    drawn = _render((home / RT.output_relpath).read_bytes(), hash_)
    assert f"than {label} ago" in drawn["title"]
    assert 'aria-pressed="true">' + label in drawn["sw"]
    cards = drawn["cards"]
    for title in ("Landing and merges", "Alarms and lab health", "Rebuild pieces"):
        assert f"<h2>{title}</h2>" in cards
    assert cards.count('<section class="card') >= 3
    landing = cards.split("Landing and merges")[1].split("</section>")[0]
    if better:
        assert f"<b>{better}</b><span>better</span>" in landing
    else:
        assert "No reading from 30 days ago" in cards


def test_the_details_are_closed_and_hold_the_whole_window_behind_the_disclosure(
    tmp_path: Path,
) -> None:
    drawn = _render(_many_doc(60), "")
    assert drawn["detailsOpen"] is False
    assert "60 numbers" in drawn["dsum"]
    assert "Landing and merges" in drawn["cat"]


def test_failing_control_the_details_are_paged_twenty_five_to_a_page() -> None:
    steps = [
        {"on": "dbody", "type": "click", "dataset": {"act": "next"}},
        {"on": "dbody", "type": "click", "dataset": {"act": "next"}},
        {"on": "dbody", "type": "click", "dataset": {"act": "next"}},
        {"on": "dbody", "type": "click", "dataset": {"act": "prev"}},
    ]
    first, second, third, over, back = _render_steps(_many_doc(60), "", steps)
    assert _rows(first["dbody"]) == 25
    assert "Page 1 of 3" in first["dbody"]
    assert "showing 1 to 25" in first["dbody"]
    assert '<button type="button" data-act="prev" disabled>' in first["dbody"]
    assert _rows(second["dbody"]) == 25
    assert "Page 2 of 3" in second["dbody"]
    assert "showing 26 to 50" in second["dbody"]
    assert _rows(third["dbody"]) == 10
    assert "Page 3 of 3" in third["dbody"]
    assert '<button type="button" data-act="next" disabled>' in third["dbody"]
    # A click past the last page stays on it; Previous goes back one.
    assert "Page 3 of 3" in over["dbody"]
    assert "Page 2 of 3" in back["dbody"]
    # A page of fewer numbers than a page has no pager at all.
    small = _render(_many_doc(10), "")
    assert _rows(small["dbody"]) == 10
    assert "Page 1 of" not in small["dbody"]


def test_search_filters_by_name_or_category_and_returns_to_the_first_page() -> None:
    steps = [
        {"on": "dbody", "type": "click", "dataset": {"act": "next"}},
        {"on": "q", "type": "input", "value": "repo012"},
        {"on": "q", "type": "input", "value": "landing and"},
        {"on": "q", "type": "input", "value": "zzz-no-such-number"},
        {"on": "dbody", "type": "click", "dataset": {"act": "clear"}},
    ]
    moved, one, by_category, none, cleared = _render_steps(_many_doc(60), "", steps)[1:]
    assert "Page 2 of 3" in moved["dbody"]
    assert _rows(one["dbody"]) == 1
    assert "repo012" in one["dbody"]
    assert "1 of 60 numbers match" in one["dbody"]
    assert "Page 1" not in one["dbody"]
    assert "60 numbers" in by_category["dbody"]
    assert "Page 1 of 3" in by_category["dbody"]
    assert _rows(none["dbody"]) == 0
    assert "No number matches." in none["dbody"]
    assert _rows(cleared["dbody"]) == 25
    assert "Page 1 of 3" in cleared["dbody"]


def test_the_category_filter_the_result_filter_and_the_sort() -> None:
    comparison = build_comparison(
        (snap(1, PLANTED_THEN),),
        snap(0, PLANTED_NOW),
        RT,
        OVERLAY,
    )
    steps = [
        {"on": "cat", "type": "change", "value": "alarms"},
        {"on": "cat", "type": "change", "value": ""},
        {"on": "ver", "type": "change", "value": "unread"},
        {"on": "ver", "type": "change", "value": ""},
        {"on": "sort", "type": "change", "value": "small"},
        {"on": "sort", "type": "change", "value": "name"},
    ]
    start, alarms, _, unread, _, small, by_name = _render_steps(
        _doc(comparison), "", steps
    )
    assert _rows(start["dbody"]) == 7
    assert _rows(alarms["dbody"]) == 2
    assert "red_rows" not in alarms["dbody"]
    assert "Red rows on the floor" in alarms["dbody"]
    assert "Alarm deploy" in alarms["dbody"]
    assert _rows(unread["dbody"]) == 2
    assert '<span class="unread">unread</span>' in unread["dbody"]
    names = re.findall(r'<td class="nm">([^<]*)</td>', by_name["dbody"])
    assert names == sorted(names)
    # Biggest change first starts with the biggest move; smallest change first starts with the smallest.
    first = re.findall(r'<td class="nm">([^<]*)</td>', start["dbody"])[0]
    smallest = re.findall(r'<td class="nm">([^<]*)</td>', small["dbody"])[0]
    assert first != smallest
    assert first.startswith("Rebuild piece")


def test_a_card_opens_the_details_on_its_own_category() -> None:
    steps = [
        {"on": "cards", "type": "click", "dataset": {"act": "see", "cat": "pieces"}}
    ]
    comparison = build_comparison(
        (snap(1, PLANTED_THEN),), snap(0, PLANTED_NOW), RT, OVERLAY
    )
    before, after = _render_steps(_doc(comparison), "", steps)
    assert before["detailsOpen"] is False
    assert after["detailsOpen"] is True
    assert after["catValue"] == "pieces"
    assert _rows(after["dbody"]) == 1
    assert "Rebuild piece 3" in after["dbody"]


def test_the_window_switch_redraws_the_cards_and_the_details_for_that_window() -> None:
    comparison = build_comparison(
        (snap(1, PLANTED_THEN),), snap(0, PLANTED_NOW), RT, OVERLAY
    )
    day, week = _render_steps(
        _doc(comparison),
        "",
        [{"on": "sw", "type": "click", "dataset": {"act": "days", "days": "7"}}],
    )
    assert 'aria-pressed="true">1 day' in day["sw"]
    assert 'aria-pressed="true">7 days' in week["sw"]
    assert "No reading from 7 days ago" in week["cards"]
    assert "7 days ago" in week["dbody"]


def test_failing_control_a_planted_gap_is_drawn_unread_never_better_or_worse(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    _compare(home, (snap(1, {"open_prs": 12}),))  # 7 days ago is a gap
    drawn = _render((home / RT.output_relpath).read_bytes(), "#7")
    landing = drawn["cards"].split("Landing and merges")[1].split("</section>")[0]
    assert "<b>0</b><span>better</span>" in landing
    assert "<b>0</b><span>worse</span>" in landing
    assert "No reading from 7 days ago" in landing
    assert "unread" in landing


def test_without_the_overlay_the_page_says_why_and_opens_the_details() -> None:
    drawn = _render(
        _many_doc(30, overlay=None).replace(
            b'"categories_error":""',
            b'"categories_error":"the overlay is not configured."',
        ),
        "",
    )
    assert (
        "The category cards are not drawn: the overlay is not configured."
        in drawn["alert"]
    )
    assert drawn["detailsOpen"] is True
    assert _rows(drawn["dbody"]) == 25


def test_an_older_comparison_file_without_the_rollup_still_draws() -> None:
    old = json.loads(_many_doc(5))
    for window in old["windows"]:
        del window["categories"], window["details"]
    del old["details_page_size"], old["categories_error"]
    drawn = _render(json.dumps(old).encode(), "")
    assert "written before the category roll-up existed" in drawn["cards"]
    assert "carries no details yet" in drawn["dbody"]


def test_a_missing_comparison_draws_a_failure_not_an_empty_page() -> None:
    assert "cannot be read" in _render(None, "")["alert"]
